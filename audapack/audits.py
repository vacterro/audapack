"""Audit indexing, validation, freshness/temperature calculation, and copy state tracking."""

from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Optional, Union

from audapack.bridge.storage import (
    atomic_write,
    generate_canonical_all3,
    generate_canonical_campaign,
    parse_wave,
    sanitize_project_name,
)
from audapack.campaign import (
    CampaignProfile,
    get_default_profile,
    load_profiles,
)
from audapack.config import AppConfig, AuditsConfig
from audapack.models import AuditSnapshot, AuditTemperature, Project

_RE_GEN_AT = re.compile(r"^GENERATED_AT:\s*(.+)$", re.MULTILINE)
_RE_DATE_TIME = re.compile(r"^DATE_TIME:\s*(.+)$", re.MULTILINE)
_RE_TOTAL_TICKETS = re.compile(r"^(?:TOTAL_TICKETS|TOTAL_RAW_TICKETS|ROOT_TICKETS):\s*(\d+)", re.MULTILINE)


def parse_iso_or_custom_datetime(raw: str) -> Optional[datetime]:
    raw = raw.strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone().replace(tzinfo=None)
        return dt
    except Exception:
        pass

    formats = (
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H-%M-%S",
        "%d-%m-%Y-T%H-%M-%S",
        "%d-%m-%Y %H:%M:%S",
        "%d.%m.%y-T%H-%M-%S",
        "%d.%m.%Y-T%H-%M-%S",
    )
    for fmt in formats:
        try:
            return datetime.strptime(raw[:19], fmt)
        except Exception:
            pass
    return None


def extract_audit_metadata_timestamp(content: str) -> Optional[datetime]:
    m_gen = _RE_GEN_AT.search(content)
    if m_gen:
        dt = parse_iso_or_custom_datetime(m_gen.group(1))
        if dt:
            return dt

    m_dt = _RE_DATE_TIME.search(content)
    if m_dt:
        dt = parse_iso_or_custom_datetime(m_dt.group(1))
        if dt:
            return dt

    return None


def is_wave_complete(
    content: str,
    wave_type_or_id: str,
    profile: Optional[Union[str, CampaignProfile]] = None,
) -> bool:
    """Readiness gate for wave markdown on disk using canonical parse_wave validation."""
    if not content or len(content) < 40:
        return False
    ok, _meta, _err = parse_wave(content, wave_type_or_id, profile)
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


def is_super_campaign_ready(final_content: str, all_content: str = "") -> tuple[bool, int]:
    """Validates if SUPER10 final handoff (__00_SUPER_AUDIT_FINAL.md or __00_SUPER_AUDIT_ALL.md) is complete."""
    content = final_content or all_content
    if not content or len(content) < 100:
        return False, 0

    m_tot = _RE_TOTAL_TICKETS.search(content)
    total_tickets = int(m_tot.group(1)) if m_tot else 0

    has_super_status = "SUPER_AUDIT_STATUS: COMPLETE" in content or "STATUS: SUPER_AUDIT_ALL: COMPLETE" in content
    has_redteam = "STATUS: AUDIT_REDTEAM: COMPLETE" in content or "AUDIT REDTEAM" in content or "FINAL DEDUPLICATED IMPLEMENTATION HANDOFF" in content
    has_done = "SUPER_AUDIT_DONE_WHEN:" in content or "SUPER_AUDIT_ALL_DONE_WHEN:" in content or "RED_DONE_WHEN:" in content

    ready = bool((has_super_status or has_redteam) and has_done)
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
        self._cache: dict[str, tuple[AuditSnapshot, dict[str, tuple[int, int]]]] = {}
        self._dir_cache: dict[str, tuple[Optional[Path], int]] = {}
        self._dir_cache_root_mtime: int = 0
        self._batch_index: Optional[dict[str, Path]] = None
        self._batch_index_mtime: int = 0
        self._profiles = load_profiles()

    def invalidate(self, project_id: Optional[str] = None):
        if project_id:
            self._cache.pop(project_id, None)
            self._dir_cache.pop(project_id, None)
        else:
            self._cache.clear()
            self._dir_cache.clear()
            self._batch_index = None
            self._batch_index_mtime = 0

    def _get_root_mtime_ns(self, root: Path) -> int:
        try:
            return root.stat().st_mtime_ns
        except Exception:
            return 0

    def _ensure_batch_index(self, root: Path) -> dict[str, Path]:
        mtime = self._get_root_mtime_ns(root)
        if self._batch_index is not None and self._batch_index_mtime == mtime:
            return self._batch_index
        index: dict[str, Path] = {}
        try:
            with os.scandir(root) as group_it:
                for g_entry in group_it:
                    if not g_entry.is_dir() or g_entry.name.startswith((".", "_")):
                        continue
                    g_path = Path(g_entry.path)
                    try:
                        with os.scandir(g_path) as proj_it:
                            for p_entry in proj_it:
                                if p_entry.is_dir():
                                    index[p_entry.name.lower()] = Path(p_entry.path)
                                    stripped = p_entry.name.lstrip("_").lower()
                                    if stripped and stripped not in index:
                                        index[stripped] = Path(p_entry.path)
                    except Exception:
                        pass
        except Exception:
            pass
        try:
            with os.scandir(root) as it:
                for entry in it:
                    if entry.is_dir() and entry.name.lower() not in index:
                        if entry.name.upper().startswith(("MAIN", "SIDE")):
                            continue
                        index[entry.name.lower()] = Path(entry.path)
                        stripped = entry.name.lstrip("_").lower()
                        if stripped and stripped not in index:
                            index[stripped] = Path(entry.path)
        except Exception:
            pass
        self._batch_index = index
        self._batch_index_mtime = mtime
        return index

    def find_project_audit_dir(self, project: Project) -> Optional[Path]:
        root = Path(self.config.audits.root)
        if not root.exists() or not root.is_dir():
            return None

        root_mtime = self._get_root_mtime_ns(root)
        cached = self._dir_cache.get(project.id)
        if cached is not None:
            cached_path, cached_mtime = cached
            if cached_mtime == root_mtime:
                if cached_path is None:
                    return None
                if cached_path.exists() and cached_path.is_dir():
                    return cached_path

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

        sanitized_extras = []
        for n in list(names_to_try):
            s = sanitize_project_name(n)
            if s and s not in names_to_try and s not in sanitized_extras:
                sanitized_extras.append(s)
            s_stripped = s.lstrip("_")
            if s_stripped and s_stripped not in names_to_try and s_stripped not in sanitized_extras:
                sanitized_extras.append(s_stripped)
        names_to_try.extend(sanitized_extras)

        grp = project.priority_group.upper()
        grp_dir = root / grp
        if grp_dir.exists() and grp_dir.is_dir():
            for name in names_to_try:
                candidate = grp_dir / name
                if candidate.exists() and candidate.is_dir():
                    self._dir_cache[project.id] = (candidate, root_mtime)
                    return candidate
            batch = self._ensure_batch_index(root)
            for name in names_to_try:
                hit = batch.get(name.lower())
                if hit is not None and hit.parent.name.upper() == grp and hit.is_dir():
                    self._dir_cache[project.id] = (hit, root_mtime)
                    return hit

        batch = self._ensure_batch_index(root)
        for name in names_to_try:
            hit = batch.get(name.lower())
            if hit is not None and hit.exists() and hit.is_dir():
                self._dir_cache[project.id] = (hit, root_mtime)
                return hit

        AUDIT_COUNTERS["directory_scans"] += 1
        if grp_dir.exists() and grp_dir.is_dir():
            try:
                for child in grp_dir.iterdir():
                    if child.is_dir() and any(child.name.lower() == n.lower() for n in names_to_try):
                        p = Path(child)
                        self._dir_cache[project.id] = (p, root_mtime)
                        return p
            except Exception:
                pass
        try:
            for g_child in root.iterdir():
                if g_child.is_dir() and g_child.name != grp:
                    for name in names_to_try:
                        candidate = g_child / name
                        if candidate.exists() and candidate.is_dir():
                            self._dir_cache[project.id] = (candidate, root_mtime)
                            return candidate
        except Exception:
            pass
        for name in names_to_try:
            candidate = root / name
            if candidate.exists() and candidate.is_dir():
                self._dir_cache[project.id] = (candidate, root_mtime)
                return candidate

        self._dir_cache[project.id] = (None, root_mtime)
        return None

    def _get_dir_signatures(self, audit_dir: Optional[Path]) -> dict[str, tuple[int, int]]:
        sigs: dict[str, tuple[int, int]] = {}
        if not audit_dir or not audit_dir.exists() or not audit_dir.is_dir():
            return sigs
        try:
            with os.scandir(audit_dir) as it:
                for entry in it:
                    if entry.is_file() and (entry.name.lower().endswith(".md") or entry.name.lower().endswith(".json")):
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

        current_sigs = self._get_dir_signatures(audit_dir)
        cached = self._cache.get(project.id)
        if cached:
            cached_snap, cached_sigs = cached
            if current_sigs == cached_sigs:
                latest_timestamp = cached_snap.audit_timestamp
                age_seconds: Optional[float] = None
                if latest_timestamp:
                    age_seconds = max(0.0, (current_time - latest_timestamp).total_seconds())
                temp = (
                    calculate_temperature(age_seconds, self.config.audits)
                    if (cached_snap.completed_waves > 0 or cached_snap.campaign_complete or cached_snap.all3_ready)
                    else AuditTemperature.NONE
                )
                return AuditSnapshot(
                    project_id=cached_snap.project_id,
                    project_name=cached_snap.project_name,
                    audit_profile_id=cached_snap.audit_profile_id,
                    audit_profile_version=cached_snap.audit_profile_version,
                    completed_waves=cached_snap.completed_waves,
                    total_waves=cached_snap.total_waves,
                    campaign_complete=cached_snap.campaign_complete,
                    final_handoff_ready=cached_snap.final_handoff_ready,
                    final_handoff_sha256=cached_snap.final_handoff_sha256,
                    final_handoff_path=cached_snap.final_handoff_path,
                    all_path=cached_snap.all_path,
                    campaign_run_id=cached_snap.campaign_run_id,
                    wave_files=cached_snap.wave_files,
                    wave_statuses=cached_snap.wave_statuses,
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
                    total_tickets=cached_snap.total_tickets,
                )

        # Detect profile from directory contents
        has_super10_markers = any(
            "AUDIT_ARCHITECTURE" in f or "AUDIT_CORRECTNESS" in f or "SUPER_AUDIT" in f
            for f in current_sigs.keys()
        )
        profile_id = "super10" if has_super10_markers else "quick3"
        profile = self._profiles.get(profile_id) or get_default_profile()

        wave_files: dict[str, Path] = {}
        wave_statuses: dict[str, bool] = {}
        wave_texts: dict[str, str] = {}
        timestamps: list[datetime] = []

        all_file: Optional[Path] = None
        final_file: Optional[Path] = None
        all3_file: Optional[Path] = None

        for f_name in current_sigs.keys():
            if f_name.startswith("."):
                continue
            f_path = audit_dir / f_name

            if "00_SUPER_AUDIT_FINAL" in f_name or "00__SUPER_AUDIT_FINAL" in f_name:
                final_file = f_path
            elif "00_SUPER_AUDIT_ALL" in f_name or "00__SUPER_AUDIT_ALL" in f_name:
                all_file = f_path
            elif "00_AUDIT_ALL_3" in f_name or "00__AUDIT_ALL_3" in f_name:
                all3_file = f_path

            for w in profile.waves:
                pattern1 = f"{w.number}_{w.slug}"
                pattern2 = f"{w.number}__{w.slug}"
                if pattern1 in f_name or pattern2 in f_name:
                    wave_files[w.id] = f_path

        total_tickets = 0
        campaign_run_id = ""

        # Validate each wave file for active profile
        for w in profile.waves:
            wf = wave_files.get(w.id)
            if wf and wf.exists():
                try:
                    text = wf.read_text(encoding="utf-8")
                    AUDIT_COUNTERS["files_read"] += 1
                    ok = is_wave_complete(text, w.id, profile)
                    wave_statuses[w.id] = ok
                    if ok:
                        wave_texts[w.id] = text
                        _, meta, _ = parse_wave(text, w.id, profile)
                        if meta:
                            total_tickets += int(meta.get("tickets", 0))
                            if not campaign_run_id and meta.get("campaign_run_id"):
                                campaign_run_id = meta["campaign_run_id"]
                        ts = extract_audit_metadata_timestamp(text) or datetime.fromtimestamp(wf.stat().st_mtime)
                        if ts:
                            timestamps.append(ts)
                except Exception:
                    wave_statuses[w.id] = False
            else:
                wave_statuses[w.id] = False

        completed_count = sum(1 for ok in wave_statuses.values() if ok)
        campaign_complete = (completed_count == profile.wave_count and profile.wave_count > 0)

        final_ready = False
        final_sha256 = ""

        if profile_id == "quick3":
            if all3_file and all3_file.exists():
                try:
                    a_text = all3_file.read_text(encoding="utf-8")
                    AUDIT_COUNTERS["files_read"] += 1
                    all3_ready, all3_tix = is_all3_ready(a_text)
                    if all3_ready:
                        final_ready = True
                        final_sha256 = hashlib.sha256(a_text.encode("utf-8")).hexdigest()
                        total_tickets = all3_tix
                        ts = extract_audit_metadata_timestamp(a_text)
                        if ts:
                            timestamps.insert(0, ts)
                        else:
                            timestamps.append(datetime.fromtimestamp(all3_file.stat().st_mtime))
                except Exception:
                    pass

            # Auto-synthesize quick3 ALL_3 if all 3 waves exist and ALL_3 missing
            if campaign_complete and (not all3_file or not final_ready):
                try:
                    parsed_d = {}
                    for w in profile.waves:
                        _, meta, _ = parse_wave(wave_texts.get(w.id, ""), w.id, profile)
                        parsed_d[w.id] = meta or {}
                    all3_target = audit_dir / f"{audit_dir.name}__00_AUDIT_ALL_3.md"
                    all3_c = generate_canonical_all3(audit_dir.name, campaign_run_id or f"synthesized_{int(current_time.timestamp())}", parsed_d)
                    atomic_write(all3_target, all3_c)
                    all3_file = all3_target
                    final_ready, total_tickets = is_all3_ready(all3_c)
                    final_sha256 = hashlib.sha256(all3_c.encode("utf-8")).hexdigest()
                    timestamps.insert(0, current_time)
                    current_sigs = self._get_dir_signatures(audit_dir)
                except Exception:
                    pass

            final_file = all3_file
            all_file = all3_file

        else:
            # SUPER10 / N-wave
            if final_file and final_file.exists():
                try:
                    f_text = final_file.read_text(encoding="utf-8")
                    AUDIT_COUNTERS["files_read"] += 1
                    s_ready, s_tix = is_super_campaign_ready(f_text)
                    if s_ready:
                        final_ready = True
                        final_sha256 = hashlib.sha256(f_text.encode("utf-8")).hexdigest()
                        if s_tix > 0:
                            total_tickets = s_tix
                        ts = extract_audit_metadata_timestamp(f_text)
                        if ts:
                            timestamps.insert(0, ts)
                        else:
                            timestamps.append(datetime.fromtimestamp(final_file.stat().st_mtime))
                except Exception:
                    pass

            if not final_ready and all_file and all_file.exists():
                try:
                    a_text = all_file.read_text(encoding="utf-8")
                    AUDIT_COUNTERS["files_read"] += 1
                    s_ready, s_tix = is_super_campaign_ready("", a_text)
                    if s_ready:
                        final_ready = True
                        final_sha256 = hashlib.sha256(a_text.encode("utf-8")).hexdigest()
                        if s_tix > 0:
                            total_tickets = s_tix
                        ts = extract_audit_metadata_timestamp(a_text)
                        if ts:
                            timestamps.insert(0, ts)
                except Exception:
                    pass

            # Auto-synthesize SUPER10 files if all waves are complete on disk
            if campaign_complete and (not final_file or not final_ready):
                try:
                    parsed_d = {}
                    for w in profile.waves:
                        _, meta, _ = parse_wave(wave_texts.get(w.id, ""), w.id, profile)
                        parsed_d[w.id] = meta or {}
                    synth_map = generate_canonical_campaign(
                        profile,
                        campaign_run_id or f"synthesized_{int(current_time.timestamp())}",
                        parsed_d,
                        audit_dir.name,
                    )
                    all_target = audit_dir / f"{audit_dir.name}__00_SUPER_AUDIT_ALL.md"
                    final_target = audit_dir / f"{audit_dir.name}__00_SUPER_AUDIT_FINAL.md"
                    idx_target = audit_dir / f"{audit_dir.name}__00_SUPER_AUDIT_INDEX.json"

                    atomic_write(all_target, synth_map.get("super_all", ""))
                    atomic_write(final_target, synth_map.get("super_final", ""))
                    atomic_write(idx_target, synth_map.get("super_index", ""))

                    all_file = all_target
                    final_file = final_target

                    final_ready, _ = is_super_campaign_ready(synth_map.get("super_final", ""))
                    final_sha256 = hashlib.sha256(synth_map.get("super_final", "").encode("utf-8")).hexdigest()
                    timestamps.insert(0, current_time)
                    current_sigs = self._get_dir_signatures(audit_dir)
                except Exception:
                    pass

        latest_timestamp = max(timestamps) if timestamps else None
        age_seconds: Optional[float] = None
        if latest_timestamp:
            age_seconds = max(0.0, (current_time - latest_timestamp).total_seconds())

        temp = (
            calculate_temperature(age_seconds, self.config.audits)
            if (completed_count > 0 or final_ready or campaign_complete)
            else AuditTemperature.NONE
        )

        core_file = wave_files.get("core") or wave_files.get("architecture")
        second_file = wave_files.get("second") or wave_files.get("correctness")
        perf_file = wave_files.get("performance")

        snap = AuditSnapshot(
            project_id=project.id,
            project_name=project.display_name,
            audit_profile_id=profile.profile_id,
            audit_profile_version=profile.profile_version,
            completed_waves=completed_count,
            total_waves=profile.wave_count,
            campaign_complete=campaign_complete,
            final_handoff_ready=final_ready,
            final_handoff_sha256=final_sha256,
            final_handoff_path=final_file,
            all_path=all_file,
            campaign_run_id=campaign_run_id,
            wave_files=wave_files,
            wave_statuses=wave_statuses,
            # Legacy compatibility fields
            core_path=core_file,
            core_complete=wave_statuses.get("core", False) or wave_statuses.get("architecture", False),
            second_path=second_file,
            second_complete=wave_statuses.get("second", False) or wave_statuses.get("correctness", False),
            performance_path=perf_file,
            performance_complete=wave_statuses.get("performance", False),
            all3_path=all3_file,
            all3_ready=final_ready if profile_id == "quick3" else False,
            all3_sha256=final_sha256 if profile_id == "quick3" else "",
            audit_dir=audit_dir,
            audit_timestamp=latest_timestamp,
            audit_age_seconds=age_seconds,
            temperature=temp,
            total_tickets=total_tickets,
        )

        self._cache[project.id] = (snap, current_sigs)
        return snap

    def scan_all_projects(self, now: Optional[datetime] = None) -> dict[str, AuditSnapshot]:
        results: dict[str, AuditSnapshot] = {}
        for p in self.config.projects:
            results[p.id] = self.scan_project(p, now=now)
        return results

    def get_preferred_audit_file_path(self, snapshot: AuditSnapshot) -> Optional[Path]:
        """Returns the Path to the preferred final handoff or newest completed/existing wave file."""
        target_file = snapshot.final_handoff_path or snapshot.all_path or snapshot.all3_path
        if not target_file or not target_file.exists():
            if snapshot.wave_files:
                for w_id in reversed(list(snapshot.wave_files.keys())):
                    if snapshot.wave_statuses.get(w_id):
                        cand = snapshot.wave_files.get(w_id)
                        if cand and cand.exists():
                            target_file = cand
                            break
                if not target_file or not target_file.exists():
                    for cand in reversed(list(snapshot.wave_files.values())):
                        if cand and cand.exists():
                            target_file = cand
                            break
        a_dir = getattr(snapshot, "audit_dir", None)
        if (not target_file or not target_file.exists()) and a_dir and a_dir.exists():
            md_files = sorted(a_dir.glob("*.md"), key=lambda f: f.stat().st_mtime, reverse=True)
            if md_files:
                target_file = md_files[0]
        return target_file if (target_file and target_file.exists()) else None

    def read_preferred_handoff(self, snapshot: AuditSnapshot) -> tuple[bool, str, str]:
        """Reads the preferred final handoff text (or newest completed wave) from disk and computes its SHA-256."""
        target_file = self.get_preferred_audit_file_path(snapshot)
        if not target_file or not target_file.exists():
            return False, "", ""
        try:
            content = target_file.read_text(encoding="utf-8")
            AUDIT_COUNTERS["files_read"] += 1
            sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
            return True, content, sha256
        except Exception:
            return False, "", ""

    def read_exact_all3(self, snapshot: AuditSnapshot) -> tuple[bool, str, str]:
        """Reads exact ALL_3 or preferred campaign handoff text from disk (backwards compatibility)."""
        return self.read_preferred_handoff(snapshot)
