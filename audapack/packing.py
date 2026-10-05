"""Packing engine for AUDAPACK.

Creates clean, verified zip archives with .part staging, exclude filtering,
Zip64 support, and optional manifest generation.
"""

from __future__ import annotations

import fnmatch
import hashlib
import heapq
import json
import os
import re
import stat as stat_module
import threading
import time
import uuid
import zipfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

from audapack import archive_receipt, saipen_evidence, saipen_manifest_gate
from audapack.config import (
    DEFAULT_OUTPUT_LAYOUT,
    OUTPUT_LAYOUT_ALONGSIDE_PROJECTS,
    OUTPUT_LAYOUT_GROUPED_BY_PRIORITY,
    PackingConfig,
    cross_process_lock,
    get_state_dir,
    normalize_output_layout,
)
from audapack.fidelity import (
    DEFAULT_PROFILE,
    LARGEST_INCLUDED_DIRECTORIES_LIMIT,
    LARGEST_INCLUDED_LIMIT,
    LARGEST_OMITTED_LIMIT,
    LOSSY_POLICY_REASONS,
    PROFILE_FULL,
    FidelityPlan,
    archive_semantics_for,
    exclude_reason_summary,
    failure_summary,
    normalize_fidelity_profile,
    packing_policy_fingerprint,
    profile_budget_bytes,
    pruned_census_totals,
)
from audapack.models import PackResult, Project
from audapack.source_inventory import (
    RESERVED_ARCHIVE_NAMES,
    SourceInventory,
    SourceInventoryError,
    build_pack_inventory,
    nested_component_manifest_section,
)

MANIFEST_FILENAME = "_AUDAPACK_MANIFEST.json"

#: T-190 (SRC-046): the canonical source-inventory manifest carried by every
#: archive written from a frozen inventory. Reserved archive metadata: source
#: files may never own this path (tracked conflicts fail the pack), and it is
#: excluded from expected/actual source parity counts.
INVENTORY_MANIFEST_PATH = ".audapack/manifest.json"
INVENTORY_MANIFEST_SCHEMA_VERSION = 1

#: CORE-006 (audit/6.md): a reuse decision reads a manifest written by an earlier
#: build, so the schema it was written against is part of what it must prove.
#: Bumped whenever a key a consumer depends on changes meaning.
#: 3 -- PERF-003: ``media_inventory`` is keyed per media GROUP
#: (``"<directory>#<class>"``) and carries exact aggregates plus bounded filename
#: samples instead of one record per media asset.
MANIFEST_SCHEMA_VERSION = 3

# Mandatory excludes — must never be packaged even if user removes them from config.
# Mirrors audapack.config.MANDATORY_EXCLUDES; kept local to avoid import cycle in tests.
MANDATORY_EXCLUDES = {
    "__pycache__",
    "*.pyc",
    "*.pyo",
    "*.pycx",
    ".pytest_cache",
    ".workbuddy-ai",
    "*.pre-redact",
    "*.secret",
    "*.secrets",
    "token.txt",
    "*.token",
    "*.pid",
    "secrets",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "*.ppk",
    "_AUDAPACK_MANIFEST.json",
}


#: PERF-002 (audit/6.md): extensions whose bytes are already entropy-coded, so
#: Deflate burns CPU for nothing. Measured on a 32 MiB precompressed payload:
#: DEFLATED write 0.698 s / 33,564,786 bytes against STORED 0.029 s /
#: 33,554,546 bytes -- 23.9x slower for an archive 0.03% LARGER.
#:
#: Conservative by construction, and NOT "all media": WAV and other raw assets
#: are deliberately absent because Deflate wins materially on them. An unknown
#: extension stays DEFLATED, so the fallback is the old behaviour.
PRECOMPRESSED_EXTENSIONS = frozenset({
    # image (lossy/entropy-coded containers)
    "jpg", "jpeg", "png", "webp", "gif",
    # video
    "mp4", "m4v", "webm", "mkv", "mov",
    # audio (raw PCM formats like wav/aiff excluded on purpose)
    "mp3", "aac", "m4a", "ogg", "opus", "flac",
    # fonts: woff carries zlib, woff2 carries brotli
    "woff", "woff2",
    # containers that already hold compressed members
    "zip", "gz", "tgz", "bz2", "xz", "zst", "7z", "rar", "whl", "jar",
})


def compress_type_for(name: str) -> int:
    """The storage method for ``name``: STORED when its bytes are precompressed.

    PERF-002: one deterministic policy for every archive-writing branch instead
    of extension checks scattered through them. Extension-only and
    case-insensitive, so the same file always lands the same way -- storage
    method is a CPU decision, never an archive-semantics one.
    """
    base = str(name).rpartition("/")[2].rpartition("\\")[2]
    dot = base.rfind(".")
    # dot > 0 matches Path.suffix: a leading-dot name (".gitignore") has none.
    if dot > 0 and base[dot + 1:].lower() in PRECOMPRESSED_EXTENSIONS:
        return zipfile.ZIP_STORED
    return zipfile.ZIP_DEFLATED


class PackingCancelled(Exception):
    pass


# CORE-001 (audit/1.md): moving the previous archive aside is the rollback
# authority for the whole transaction, so a transient Windows sharing violation
# on that ONE move must not cost the operator a repack. Same bounded shape as
# config.py's `_WINDOWS_FILE_RETRY_ATTEMPTS`; retried on every platform because
# the failure being survived (another process holding the file) is not
# Windows-only, and a handful of 20-80 ms sleeps cost nothing next to zipping.
_BACKUP_ESTABLISH_ATTEMPTS = 4


# CORE-001: full-transaction cross-process locks keyed by (output_dir, stem).
# Replaces the previous retention-only in-process lock so a failing pack can
# never delete or restore over output produced by another successful concurrent
# transaction. The lock file lives under the canonical state directory so all
# processes sharing the runtime coordinate on the same primitive.
def _pack_transaction_lock_path(output_dir: Path, stem: str) -> Path:
    key = f"{Path(output_dir).resolve()}|{safe_archive_stem(stem)}"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
    return get_state_dir() / "pack_locks" / f"pack_{digest}.lock"


def safe_archive_stem(name: str) -> str:
    name = name.strip()
    name = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "_", name)
    name = name.rstrip(" .")
    return name or "Archive"


# Timestamped-history form: {stem}_DD.MM.YY-THH-MM-SS.zip or legacy DD-MM-YYYY variant
# Anchored so a sibling project named "{stem}_Bar" can never match "{stem}".
_ARCHIVE_HISTORY_RE = re.compile(r"^\d{2}[.\-]\d{2}[.\-]\d{2,4}-T\d{2}-\d{2}-\d{2}(?:-\d{6})?(?:-\d+)?\.zip$")


def archive_belongs_to_stem(filename: str, stem: str) -> bool:
    """True when ``filename`` is a canonical archive for ``stem``.

    Accepts the clean ``{stem}.zip`` and the anchored timestamped history form
    ``{stem}_DD.MM.YY-THH-MM-SS.zip``. A name like ``Foo_Bar_27.08.26-T00-00-00.zip``
    does NOT belong to ``Foo`` — prefix sharing must never cross project boundaries.
    """
    safe_stem = safe_archive_stem(stem)
    if not safe_stem:
        return False
    name = filename
    if name == f"{safe_stem}.zip":
        return True
    if name.startswith(f"{safe_stem}_"):
        return bool(_ARCHIVE_HISTORY_RE.match(name[len(safe_stem) + 1:]))
    return False


def human_mb(value: int) -> str:
    """Format a byte count with the largest useful binary unit.

    Keep the historical function name because it is used by the UI and pack
    status paths, but do not force sub-megabyte archives to display as 0.0 MB.
    """
    size = max(0, int(value))
    units = ("B", "KB", "MB", "GB", "TB")
    amount = float(size)
    unit_index = 0
    while amount >= 1024 and unit_index < len(units) - 1:
        amount /= 1024
        unit_index += 1

    if unit_index == 0:
        return f"{size} B"
    return f"{amount:.1f} {units[unit_index]}"


def _build_exclusion_matcher(patterns: set[str]):
    lowered = frozenset(pat.lower() for pat in patterns)
    # A pattern containing a separator names a RUN of directories, not one
    # name. ".git/objects" has to exclude the pack files without excluding
    # every directory called "objects" a project happens to have.
    multi = tuple(
        tuple(seg for seg in pat.replace("\\", "/").split("/") if seg)
        for pat in lowered
        if "/" in pat or "\\" in pat
    )
    single = {pat for pat in lowered if "/" not in pat and "\\" not in pat}
    exact = {pat for pat in single if not any(char in pat for char in "*?[")}
    globs = tuple(re.compile(fnmatch.translate(pat)) for pat in single if pat not in exact)
    multi_res = tuple(
        tuple(re.compile(fnmatch.translate(seg)) for seg in segs) for segs in multi
    )

    def _segment_matches(pattern: re.Pattern[str], part: str) -> bool:
        return bool(pattern.fullmatch(part))

    def matches(path: Path | str) -> bool:
        p = path if isinstance(path, Path) else Path(path)
        parts = tuple(part.lower() for part in p.parts)
        for part in (p.name.lower(), *parts):
            if part in exact or any(pattern.fullmatch(part) for pattern in globs):
                return True
        for segs in multi_res:
            span = len(segs)
            if span > len(parts):
                continue
            for start in range(len(parts) - span + 1):
                if all(_segment_matches(segs[i], parts[start + i]) for i in range(span)):
                    return True
        return False

    return matches


def _path_is_excluded_normalized(path: Path | str, lowered: set[str]) -> bool:
    if callable(lowered):
        return bool(lowered(path))
    return _build_exclusion_matcher(lowered)(path)


def path_is_excluded(path: Path | str, patterns: set[str]) -> bool:
    """Checks path using case-insensitive exact and fnmatch exclusions."""
    return _path_is_excluded_normalized(path, {pat.lower() for pat in patterns})


@dataclass
class ZipStats:
    """Truthful per-pack accounting.

    T-147: a file is either included, excluded with a reason category, or
    failed. ``discovered == included + excluded + failed`` always holds, so a
    file can never silently disappear from the archive without the manifest
    explaining where it went.
    """

    files_added: int = 0          # zip entries written, manifest included
    bytes_written: int = 0
    files_discovered: int = 0
    files_included: int = 0
    files_excluded: int = 0
    files_failed: int = 0
    walk_errors: int = 0
    source_bytes: int = 0
    included_bytes: int = 0
    excluded_bytes: int = 0
    #: Bytes of files that were planned or discovered but could not be read into
    #: the archive. CORE-003 (audit/6.md): without this side the byte identity is
    #: unsatisfiable the moment one file fails -- its bytes are in neither the
    #: included nor the excluded column.
    failed_bytes: int = 0
    #: Discovered entries whose size could not be read. CORE-003 (audit/6.md):
    #: an unknowable size is declared unknown instead of being counted as a known
    #: zero, so ``source_bytes`` stays a statement about measured material.
    unknown_size_entries: int = 0


def stats_accounting_error(
    stats: ZipStats, fidelity: Optional[dict] = None
) -> Optional[str]:
    """Why ``stats`` cannot be serialized as truthful accounting, or None.

    CORE-003 (audit/6.md): the plan's totals were copied straight into the
    manifest, so a planning-time accounting defect became persistent archive
    metadata. This is the final gate before emission: the identity, the byte
    reconciliation, and (with a fidelity payload) the reason totals must all
    agree with the numbers about to be written.
    """
    total = stats.files_included + stats.files_excluded + stats.files_failed
    if stats.files_discovered != total:
        return (
            f"files_discovered {stats.files_discovered} != included "
            f"{stats.files_included} + excluded {stats.files_excluded} + failed "
            f"{stats.files_failed}"
        )
    if stats.source_bytes != stats.included_bytes + stats.excluded_bytes + stats.failed_bytes:
        return (
            f"source_bytes {stats.source_bytes} != included_bytes "
            f"{stats.included_bytes} + excluded_bytes {stats.excluded_bytes} + "
            f"failed_bytes {stats.failed_bytes} (unknown-size entries: "
            f"{stats.unknown_size_entries})"
        )
    if fidelity:
        exclusions = fidelity.get("exclusions") or {}
        reason_count = sum(int(v.get("count", 0)) for v in exclusions.values())
        if reason_count != stats.files_excluded:
            return f"reason totals {reason_count} != files_excluded {stats.files_excluded}"
        reason_bytes = sum(int(v.get("bytes", 0)) for v in exclusions.values())
        if reason_bytes != stats.excluded_bytes:
            return f"reason bytes {reason_bytes} != excluded_bytes {stats.excluded_bytes}"
    return None


def generate_manifest_data(
    project_name: str,
    source_path: str,
    source_kind: str,
    stats: ZipStats,
    *,
    fidelity: Optional[dict] = None,
    extra_meta: Optional[dict] = None,
) -> dict:
    """T-147: the manifest declares WHAT the archive is, not just that it exists.

    ``fidelity`` carries the profile, archive semantics, exclusion reason
    summary, largest omitted files, pruned directories and the media inventory
    from the fidelity plan. Without a plan the packer included everything the
    explicit exclusion policy allowed, so the semantics are honestly FULL --
    the archive never implies audit_representation when nothing was trimmed.

    PERF-003: the media inventory is per-group aggregates plus a bounded name
    sample. It used to be one record per media asset, so the manifest grew with
    the tree it described.
    """
    fid_profile = (fidelity or {}).get("fidelity_profile", PROFILE_FULL)
    meta = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "product": "AUDAPACK",
        "created_at": datetime.now().isoformat(),
        "project": project_name,
        "source_path": str(source_path),
        "source_kind": source_kind,
        "fidelity_profile": fid_profile,
        "archive_semantics": (fidelity or {}).get(
            "archive_semantics", archive_semantics_for(fid_profile)
        ),
        "files_discovered": stats.files_discovered,
        "files_included": stats.files_included,
        "files_excluded": stats.files_excluded,
        "files_failed": stats.files_failed,
        "source_bytes": stats.source_bytes,
        "included_bytes": stats.included_bytes,
        "excluded_bytes": stats.excluded_bytes,
        "archive_bytes": stats.bytes_written,
    }
    # CORE-003: the archive states whether its own numbers reconcile. Both keys
    # are always present, so a reader never has to infer the guarantee from an
    # absent field, and a defect is named rather than silently serialized.
    error = stats_accounting_error(stats, fidelity)
    meta["accounting_reconciled"] = error is None
    if error:
        meta["accounting_error"] = error
    if stats.failed_bytes:
        # The third side of the byte identity: material that was measured but
        # could not be read into the archive belongs to neither column.
        meta["failed_bytes"] = stats.failed_bytes
    if stats.unknown_size_entries:
        # Sizes that could not be read are declared, so source_bytes is never
        # mistaken for a complete measurement of the tree.
        meta["unknown_size_entries"] = stats.unknown_size_entries
    if fidelity:
        meta["budget_bytes"] = fidelity.get("budget_bytes", 0)
        meta["budget_met"] = bool(fidelity.get("budget_met", True))
        if "budget_floor_bytes" in fidelity:
            meta["budget_floor_bytes"] = fidelity["budget_floor_bytes"]
        if "budget_feasible" in fidelity:
            meta["budget_feasible"] = bool(fidelity["budget_feasible"])
        if "mandatory_bytes" in fidelity:
            meta["mandatory_bytes"] = fidelity["mandatory_bytes"]
        if "discretionary_bytes" in fidelity:
            meta["discretionary_bytes"] = fidelity["discretionary_bytes"]
        if "included_bytes_by_priority" in fidelity:
            meta["included_bytes_by_priority"] = fidelity["included_bytes_by_priority"]
        if "largest_included" in fidelity:
            meta["largest_included"] = fidelity["largest_included"]
        if "largest_included_directories" in fidelity:
            meta["largest_included_directories"] = fidelity["largest_included_directories"]
        # CORE-006: an archive states the policy identity it was built under, so
        # a later reuse decision can refuse an archive whose policy has moved.
        if fidelity.get("policy_fingerprint"):
            meta["policy_fingerprint"] = fidelity["policy_fingerprint"]
        if fidelity.get("walk_incomplete"):
            meta["walk_incomplete"] = True
        if fidelity.get("exclusions"):
            meta["exclusions"] = fidelity["exclusions"]
        if fidelity.get("failures"):
            meta["failures"] = fidelity["failures"]
        if fidelity.get("largest_omitted"):
            meta["largest_omitted"] = fidelity["largest_omitted"]
        if fidelity.get("pruned_directories"):
            meta["pruned_directories"] = fidelity["pruned_directories"]
            meta["pruned_files"] = fidelity.get("pruned_files", 0)
            meta["pruned_bytes"] = fidelity.get("pruned_bytes", 0)
        if fidelity.get("media_inventory"):
            meta["media_inventory"] = fidelity["media_inventory"]
    if extra_meta:
        meta.update(extra_meta)
    return meta


def _fidelity_payload(plan) -> Optional[dict]:
    if plan is None:
        return None
    pruned_files, pruned_bytes = pruned_census_totals(plan)
    return {
        "fidelity_profile": plan.profile,
        "archive_semantics": plan.archive_semantics,
        # CORE-006: the policy that produced this archive, so freshness can be a
        # question about identity rather than only about timestamps.
        "policy_fingerprint": plan.policy_fingerprint,
        # T-147: the budget is SOFT. Mandatory audit material is never trimmed
        # to reach it, so a source-heavy project can legitimately overshoot --
        # and the manifest says so instead of implying the target was met.
        "budget_bytes": plan.budget_bytes,
        "budget_met": plan.budget_bytes == 0 or plan.included_bytes <= plan.budget_bytes,
        "budget_floor_bytes": plan.budget_floor_bytes,
        "budget_feasible": plan.budget_feasible,
        "mandatory_bytes": plan.mandatory_bytes,
        "discretionary_bytes": plan.discretionary_bytes,
        "included_bytes_by_priority": {
            str(p): b for p, b in sorted(plan.included_bytes_by_priority.items())
        },
        "largest_included": plan.largest_included,
        "largest_included_directories": plan.largest_included_directories,
        # An unreadable directory or a dead walk means the counters below cover
        # only what was reached. Declared, because the identity
        # discovered == included + excluded + failed still holds over truncated
        # numbers and would otherwise read as complete accounting.
        "walk_incomplete": plan.walk_incomplete,
        "exclusions": exclude_reason_summary(plan),
        # CORE-003: a file the tree refused to yield is not an exclusion anybody
        # chose, so failures carry their own category vocabulary.
        "failures": failure_summary(plan),
        "largest_omitted": [[rel, size] for rel, size in plan.largest_omitted],
        # Each prune reports the material it removed, so "how much was omitted
        # here" is answerable without descending into the tree again.
        "pruned_directories": [
            {
                "rel": rel,
                "reason": str(info.get("reason", "")),
                "files": int(info.get("files", 0)),
                "bytes": int(info.get("bytes", 0)),
            }
            for rel, info in sorted(plan.pruned_dirs_rel.items())
        ],
        "pruned_files": pruned_files,
        "pruned_bytes": pruned_bytes,
        "media_inventory": plan.media_inventory,
    }


# ---------------------------------------------------------------------------
# T-190 (SRC-046): frozen-inventory archive writing
#
#     DISCOVER -> VALIDATE INVENTORY -> FREEZE -> WRITE -> VERIFY -> COMMIT
#
# Membership comes ONLY from the frozen SourceInventory; the writer never
# walks the source tree, and every included file is read exactly once while
# its SHA-256 is computed from the same bytes that enter the ZIP.
# ---------------------------------------------------------------------------


#: Compact terminal pack states exposed on PackResult.status.
PACK_STATUS_PACKED = "PACKED"
PACK_STATUS_FAILED_INVENTORY = "FAILED_INVENTORY"
PACK_STATUS_FAILED_SOURCE_READ = "FAILED_SOURCE_READ"
PACK_STATUS_FAILED_SOURCE_CHANGED = "FAILED_SOURCE_CHANGED"
PACK_STATUS_FAILED_VERIFY = "FAILED_VERIFY"

#: Bounded streaming chunk for hashing + writing (bytes).
_WRITE_CHUNK = 1024 * 1024

#: A source that changed under the writer gets at most ONE full re-freeze and
#: rewrite attempt. Never an unbounded retry loop.
_SOURCE_CHANGE_MAX_ATTEMPTS = 2


class SourceReadError(Exception):
    """An inventory member could not be opened/read while packing (fail-closed)."""

    def __init__(self, rel: str, reason: str) -> None:
        super().__init__(f"{rel}: {reason}")
        self.rel = rel
        self.reason = reason


class SourceChangedError(Exception):
    """A source file changed after the inventory froze (fail-closed, one retry)."""

    def __init__(self, rel: str, reason: str) -> None:
        super().__init__(f"{rel}: {reason}")
        self.rel = rel
        self.reason = reason


class ArchiveVerifyError(Exception):
    """The staged archive failed post-write parity verification (fail-closed)."""

    def __init__(self, message: str, rel: str = "") -> None:
        super().__init__(f"{message} ({rel})" if rel else message)
        self.rel = rel
        self.message = message


def _open_source(path: Path):
    """Open one source file for packing. Test seam for read-failure injection."""
    return open(path, "rb")


def _stat_source(path: Path, *, follow: bool) -> os.stat_result:
    """Stat one source file. Test seam for change injection."""
    return os.stat(path, follow_symlinks=follow)


def _zip_date_time_for(st: os.stat_result) -> tuple[int, int, int, int, int, int]:
    """A ZIP timestamp from the frozen stat, clamped into ZIP's 1980 floor."""
    try:
        stamp = datetime.fromtimestamp(st.st_mtime)
    except (OSError, OverflowError, ValueError):
        return (1980, 1, 1, 0, 0, 0)
    if stamp.year < 1980:
        return (1980, 1, 1, 0, 0, 0)
    return (stamp.year, stamp.month, stamp.day, stamp.hour, stamp.minute, stamp.second)


@dataclass
class StagedInventoryZip:
    """Result of one staged archive write: the .part file plus write evidence."""

    part_path: Path
    stats: ZipStats
    #: rel -> (written size, sha256 hex) for every source file in the archive.
    sha256_by_rel: dict


def build_inventory_manifest_payload(
    inventory: SourceInventory,
    sha256_by_rel: dict,
    *,
    project_name: str,
) -> dict:
    """The canonical ``.audapack/manifest.json`` payload (SRC-046 contract).

    Describes the frozen inventory and the EXACT bytes written: one entry per
    included source file with path, origin, size and the SHA-256 computed
    during the write. Separate from the legacy ``_AUDAPACK_MANIFEST.json``,
    whose schema and consumers are unchanged.
    """
    files = [
        {
            "path": entry.rel,
            "origin": entry.origin,
            "size": sha256_by_rel[entry.rel][0],
            "sha256": sha256_by_rel[entry.rel][1],
        }
        for entry in inventory.included_entries()
    ]
    return {
        "schema_version": INVENTORY_MANIFEST_SCHEMA_VERSION,
        "kind": "audapack_source_inventory",
        "project_name": project_name,
        "created_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "inventory_mode": inventory.mode,
        "git_head": inventory.git_head,
        "git_dirty": bool(inventory.git_dirty),
        "counts": {
            "tracked": inventory.tracked_count,
            "untracked": inventory.untracked_count,
            "filesystem": inventory.filesystem_count,
            "included": len(files),
            "excluded": len(inventory.excluded_entries()),
            "tracked_deleted": len(inventory.tracked_deleted),
        },
        "tracked_deleted": inventory.tracked_deleted,
        # T-246: the reserved control artifacts this archive regenerated in
        # place of a tracked stale copy. Named here so the omission is always
        # visible to whoever reads the archive, never a silent hole.
        "superseded_control": inventory.superseded_control,
        # Same-product nested Git components: which direct child worktrees are
        # part of THIS product's source (packaged under their real paths) and
        # which were deliberately left out as foreign. Always present, so a
        # reader written before this feature simply sees an empty list.
        "nested_git_components": nested_component_manifest_section(inventory),
        "files": files,
    }


def _fidelity_payload_for_inventory(
    inventory: SourceInventory,
    packing,
    excludes: set[str],
) -> dict:
    """Manifest fidelity payload for a Git-mode pack (no walked plan exists).

    Same shape as :func:`_fidelity_payload` so manifest consumers see one
    schema; the numbers describe the frozen inventory instead of a walk.
    Untracked Git material never passes through media sampling or the soft
    budget trim in T-190, so no lossy-policy omission can arise here and the
    declared semantics survive unless a future policy adds one.
    """
    if packing is not None:
        profile = normalize_fidelity_profile(
            str(getattr(packing, "fidelity_profile", DEFAULT_PROFILE))
        )
        max_mb = int(getattr(packing, "fidelity_max_mb", 0) or 0)
        media_samples = int(getattr(packing, "fidelity_media_samples", 0) or 0)
        media_bytes = int(getattr(packing, "fidelity_media_bytes", 0) or 0)
        always_include = list(getattr(packing, "always_include", None) or [])
        always_exclude = list(getattr(packing, "always_exclude", None) or [])
    else:
        profile = normalize_fidelity_profile(PROFILE_FULL)
        max_mb = 0
        media_samples = 0
        media_bytes = 0
        always_include = []
        always_exclude = []

    budget = profile_budget_bytes(profile, max_mb)
    included = inventory.included_entries()
    excluded = inventory.excluded_entries()
    included_bytes = sum(e.size for e in included)

    reason_stats: dict[str, dict[str, int]] = {}
    for entry in excluded:
        reason = entry.reason or "unsupported_special_file"
        bucket = reason_stats.setdefault(reason, {"count": 0, "bytes": 0})
        bucket["count"] += 1
        bucket["bytes"] += entry.size

    by_priority: dict[int, int] = {}
    dir_bytes: dict[str, int] = {}
    for entry in included:
        by_priority[entry.priority] = by_priority.get(entry.priority, 0) + entry.size
        parent = entry.rel.rpartition("/")[0] or "."
        dir_bytes[parent] = dir_bytes.get(parent, 0) + entry.size

    semantics = archive_semantics_for(profile)
    if any(reason_stats.get(reason, {}).get("count", 0) for reason in LOSSY_POLICY_REASONS):
        semantics = "audit_representation"

    return {
        "fidelity_profile": profile,
        "archive_semantics": semantics,
        "policy_fingerprint": packing_policy_fingerprint(
            profile=profile,
            max_mb=max_mb,
            media_samples=media_samples,
            media_bytes=media_bytes,
            excludes=excludes,
            always_include=always_include,
            always_exclude=always_exclude,
        ),
        "budget_bytes": budget,
        "budget_met": budget == 0 or included_bytes <= budget,
        "budget_floor_bytes": 0,
        "budget_feasible": True,
        "mandatory_bytes": 0,
        "discretionary_bytes": 0,
        "included_bytes_by_priority": {str(p): b for p, b in sorted(by_priority.items())},
        "largest_included": [
            {"rel": e.rel, "size": e.size, "priority": e.priority}
            for e in heapq.nsmallest(
                LARGEST_INCLUDED_LIMIT,
                included,
                key=lambda e: (-e.size, e.rel.lower(), e.rel),
            )
        ],
        "largest_included_directories": [
            {"rel": rel_dir, "bytes": total_b}
            for rel_dir, total_b in heapq.nsmallest(
                LARGEST_INCLUDED_DIRECTORIES_LIMIT,
                dir_bytes.items(),
                key=lambda item: (-item[1], item[0].lower()),
            )
        ],
        "walk_incomplete": False,
        "exclusions": {k: dict(v) for k, v in sorted(reason_stats.items())},
        "failures": {},
        "largest_omitted": [
            [e.rel, e.size]
            for e in heapq.nsmallest(
                LARGEST_OMITTED_LIMIT,
                (e for e in excluded if e.size > 0),
                key=lambda e: (-e.size, e.rel.lower(), e.rel),
            )
        ],
        "pruned_directories": [],
        "pruned_files": 0,
        "pruned_bytes": 0,
        "media_inventory": {},
    }


def stage_inventory_zip(
    source_dir: str | Path,
    output_zip: Path,
    inventory: SourceInventory,
    *,
    plan: Optional[FidelityPlan] = None,
    cancel_event: Optional[threading.Event] = None,
    log_callback: Optional[Callable[[str], None]] = None,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
    manifest_meta: Optional[dict] = None,
    fidelity_payload: Optional[dict] = None,
    project_name: Optional[str] = None,
) -> StagedInventoryZip:
    """Write the frozen inventory into a unique ``.part`` staging archive.

    Never walks the source and never decides membership: every written byte
    comes from ``inventory``. Each file is streamed once; its SHA-256 is taken
    from the same bytes. Deviations from the frozen stat identity raise
    :class:`SourceChangedError`; unreadable files raise
    :class:`SourceReadError`; any failure removes the staging file. The caller
    verifies (:func:`verify_inventory_archive`) and only then commits the
    atomic rename.
    """
    source = Path(source_dir).resolve()
    output_zip.parent.mkdir(parents=True, exist_ok=True)
    part_path = output_zip.with_name(f"{output_zip.name}.part.{uuid.uuid4().hex}")

    prog = progress_callback or (lambda added, b_written, cur_path: None)
    c_event = cancel_event or threading.Event()

    stats = ZipStats()
    entries = inventory.entries
    included_entries = inventory.included_entries()
    excluded_entries = inventory.excluded_entries()
    stats.files_discovered = len(entries)
    stats.files_included = len(included_entries)
    stats.files_excluded = len(excluded_entries)
    stats.files_failed = 0
    stats.source_bytes = sum(e.size for e in entries.values())
    stats.excluded_bytes = sum(e.size for e in excluded_entries)
    if plan is not None:
        # Filesystem mode: the plan is the traversal that produced the frozen
        # inventory, so its counters (including any unknown-size declarations)
        # are the manifest accounting -- identical to the pre-inventory seeds.
        stats.files_discovered = plan.discovered
        stats.files_included = plan.included
        stats.files_excluded = plan.excluded
        stats.files_failed = plan.failed
        stats.source_bytes = plan.source_bytes
        stats.excluded_bytes = plan.excluded_bytes
        stats.unknown_size_entries = plan.unknown_size_entries

    hashes: dict[str, tuple[int, str]] = {}

    # SRC-100 Milestone F: ONE WRITER PER CONTROL PATH, asserted BEFORE a single
    # archive byte exists. The guard after the payload loop catches the same
    # collision, but only once a staging ZIP has already been filled with the
    # duplicate's first copy; the operator then pays for a full write before
    # hearing about it. Cheap here, deterministic, and it names both sides of
    # the collision -- the source path and the control writer that owns it.
    regenerated_controls = set()
    if manifest_meta is not None:
        regenerated_controls.add(MANIFEST_FILENAME)
    regenerated_controls.add(INVENTORY_MANIFEST_PATH)
    for entry in included_entries:
        if entry.rel in regenerated_controls:
            raise ArchiveVerifyError(
                f"reserved archive-control member {entry.rel!r} reached the payload as source "
                "material, but this pack writes its own control artifact under that name; "
                "refusing to write the same member twice"
            )

    try:
        with zipfile.ZipFile(part_path, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
            for entry in included_entries:
                if c_event.is_set():
                    raise PackingCancelled("Cancelled by user")
                full = source.joinpath(*entry.rel.split("/"))
                if entry.symlink:
                    # Re-prove the frozen symlink resolution: the target must
                    # still be a regular file inside the source root.
                    target = Path(os.path.realpath(full))
                    root_real = Path(os.path.realpath(source))
                    try:
                        inside = target == root_real or root_real in target.parents
                        if not inside or not target.is_file():
                            raise SourceChangedError(
                                entry.rel,
                                "symlink no longer resolves to a file inside the project root",
                            )
                        st = _stat_source(target, follow=True)
                    except OSError as exc:
                        raise SourceChangedError(
                            entry.rel, f"symlink target unreadable: {exc}"
                        ) from exc
                    read_path: Path = target
                else:
                    try:
                        st = _stat_source(full, follow=False)
                    except OSError as exc:
                        raise SourceChangedError(
                            entry.rel, f"vanished or unreadable after inventory freeze: {exc}"
                        ) from exc
                    if stat_module.S_ISDIR(st.st_mode) or stat_module.S_ISLNK(st.st_mode):
                        raise SourceChangedError(
                            entry.rel, "changed from file to directory/symlink during pack"
                        )
                    read_path = full
                if (
                    st.st_size != entry.size
                    or (entry.mtime_ns and st.st_mtime_ns != entry.mtime_ns)
                    or (entry.st_ino and entry.st_dev and (st.st_ino, st.st_dev) != (entry.st_ino, entry.st_dev))
                ):
                    raise SourceChangedError(
                        entry.rel,
                        f"changed during pack (stat identity differs from frozen inventory: "
                        f"size {st.st_size} vs {entry.size})",
                    )

                zinfo = zipfile.ZipInfo(entry.rel, date_time=_zip_date_time_for(st))
                zinfo.compress_type = compress_type_for(entry.rel)
                zinfo.external_attr = 0o100644 << 16
                hasher = hashlib.sha256()
                written = 0
                try:
                    with _open_source(read_path) as src, zf.open(zinfo, "w") as dst:
                        while True:
                            if c_event.is_set():
                                raise PackingCancelled("Cancelled by user")
                            chunk = src.read(_WRITE_CHUNK)
                            if not chunk:
                                break
                            dst.write(chunk)
                            hasher.update(chunk)
                            written += len(chunk)
                except PackingCancelled:
                    raise
                except SourceChangedError:
                    raise
                except OSError as exc:
                    raise SourceReadError(entry.rel, str(exc)) from exc
                if written != entry.size:
                    raise SourceChangedError(
                        entry.rel,
                        f"changed size while packing ({written} bytes read vs {entry.size} frozen)",
                    )
                stats.bytes_written += written
                stats.included_bytes += written
                stats.files_added += 1
                hashes[entry.rel] = (written, hasher.hexdigest())
                if stats.files_added == 1 or stats.files_added % 50 == 0:
                    prog(stats.files_added, stats.bytes_written, entry.rel)

            # Legacy AUDAPACK manifest: unchanged schema, unchanged consumers.
            if manifest_meta is not None:
                manifest_payload = generate_manifest_data(
                    project_name=manifest_meta.get("project_name", project_name or source.name),
                    source_path=str(source),
                    source_kind="file" if source.is_file() else "folder",
                    stats=stats,
                    fidelity=fidelity_payload,
                    extra_meta=manifest_meta.get("extra_meta"),
                )
                manifest_payload["inventory_mode"] = inventory.mode
                if inventory.mode == "git":
                    manifest_payload["git_head"] = inventory.git_head
                    manifest_payload["git_dirty"] = bool(inventory.git_dirty)
                    manifest_payload["git_summary"] = inventory.git_summary()
                manifest_payload["tracked_deleted"] = list(inventory.tracked_deleted)
                # The snapshot's own verdict about protocol evidence. Present
                # only for projects carrying `.saipen/`, so a non-SAIPEN
                # manifest keeps its existing shape exactly.
                #
                # `accounting_reconciled` above proves the FILE COUNTS add up.
                # It cannot prove the right files were discovered at all --
                # which is exactly how a package reported itself healthy while
                # carrying one stale intake receipt and no LOG: the missing
                # evidence was never counted, so the identity held over
                # material nobody had looked at. This key answers the question
                # the accounting cannot.
                if inventory.saipen is not None:
                    manifest_payload["saipen_snapshot"] = inventory.saipen
                manifest_bytes = json.dumps(manifest_payload, ensure_ascii=False, indent=2).encode("utf-8")
                zinfo = zipfile.ZipInfo(MANIFEST_FILENAME)
                zinfo.compress_type = compress_type_for(MANIFEST_FILENAME)
                zf.writestr(zinfo, manifest_bytes)
                stats.files_added += 1
                stats.bytes_written += len(manifest_bytes)

            # Canonical source-inventory manifest (T-190).
            # SRC-100: a reserved control name that already reached the archive
            # as payload would be written a SECOND time here. zipfile allows it
            # with only a UserWarning, and the pack then died as FAILED_VERIFY
            # with nothing naming the cause. Every inventory mode now refuses or
            # supersedes a reserved name; this is the backstop that keeps a
            # future mode from reintroducing the duplicate silently.
            if INVENTORY_MANIFEST_PATH in zf.namelist():
                raise ArchiveVerifyError(
                    f"reserved archive-control member {INVENTORY_MANIFEST_PATH!r} is already "
                    "present as source payload; refusing to write a duplicate member"
                )
            inventory_payload = build_inventory_manifest_payload(
                inventory,
                hashes,
                project_name=project_name or source.name,
            )
            inventory_bytes = json.dumps(inventory_payload, ensure_ascii=False, indent=2).encode("utf-8")
            zinfo = zipfile.ZipInfo(INVENTORY_MANIFEST_PATH)
            zinfo.compress_type = compress_type_for(INVENTORY_MANIFEST_PATH)
            zf.writestr(zinfo, inventory_bytes)
            stats.files_added += 1
            stats.bytes_written += len(inventory_bytes)

        if c_event.is_set():
            raise PackingCancelled("Cancelled by user")
        return StagedInventoryZip(part_path=part_path, stats=stats, sha256_by_rel=hashes)
    except Exception:
        if part_path.exists():
            try:
                part_path.unlink()
            except OSError:
                pass
        raise


def verify_inventory_archive(
    zip_path: Path,
    sha256_by_rel: dict,
    *,
    reserved: Optional[set] = None,
) -> None:
    """Prove exact parity between the frozen inventory and the staged ZIP.

    Both directions, on the ZIP alone (sources are never re-read):
    EXPECTED -> ZIP: every expected source path exists exactly once, is
    readable, and matches its written size and SHA-256 byte for byte.
    ZIP -> EXPECTED: no source payload path exists beyond the frozen set.
    Reserved archive-control entries (both manifests) are excluded from the
    parity sets explicitly. A valid central directory alone proves nothing:
    every member body is streamed and hashed.
    """
    reserved = RESERVED_ARCHIVE_NAMES if reserved is None else set(reserved)
    try:
        zf = zipfile.ZipFile(zip_path, "r")
    except (OSError, zipfile.BadZipFile) as exc:
        raise ArchiveVerifyError(f"staged archive is not a readable zip: {exc}") from exc
    with zf:
        names = zf.namelist()
        counts = Counter(names)
        duplicated = sorted(name for name, count in counts.items() if count > 1)
        if duplicated:
            raise ArchiveVerifyError("archive member written more than once", rel=duplicated[0])
        name_set = set(names)
        expected = set(sha256_by_rel)
        unexpected = sorted(name_set - expected - reserved)
        if unexpected:
            raise ArchiveVerifyError(
                "unexpected source payload path not in the frozen inventory",
                rel=unexpected[0],
            )
        missing = sorted(expected - name_set)
        if missing:
            raise ArchiveVerifyError("expected source path missing from the archive", rel=missing[0])
        for rel in sorted(expected):
            expected_size, expected_digest = sha256_by_rel[rel]
            hasher = hashlib.sha256()
            read_bytes = 0
            try:
                with zf.open(rel, "r") as fh:
                    while True:
                        chunk = fh.read(_WRITE_CHUNK)
                        if not chunk:
                            break
                        hasher.update(chunk)
                        read_bytes += len(chunk)
            except (OSError, zipfile.BadZipFile) as exc:
                raise ArchiveVerifyError(f"archive member unreadable: {exc}", rel=rel) from exc
            if read_bytes != expected_size:
                raise ArchiveVerifyError(
                    f"archive member size {read_bytes} != frozen {expected_size}", rel=rel
                )
            if hasher.hexdigest() != expected_digest:
                raise ArchiveVerifyError("archive member SHA-256 mismatch", rel=rel)



def eligible_source_files(
    source: Path,
    excludes: set[str],
    plan: Optional[FidelityPlan] = None,
):
    """Yield exactly the files a pack of ``source`` would put in the archive.

    One traversal contract for both the packer and anything that has to reason
    about pack inputs. PERF-004 (audit/2.md): `ensure_fresh_archive()` walked the
    tree WITHOUT the exclusion matcher and stat'ed every file below the project
    root, so deciding whether an archive could be reused traversed precisely the
    generated/cache/object trees that packing excludes for performance --
    measured 0.13 ms with nothing excluded, 38.27 ms at 5,000 excluded files,
    141.25 ms at 20,000, and the same existing archive was reused every time.
    Sharing the matcher is also what stops the two algorithms drifting apart
    again.

    T-147: when a ``plan`` is supplied, the profile's decisions (media budget,
    size limit, always_exclude, pruned directories) are honored too, so a file
    the profile would exclude can never invalidate a fresh archive.

    Symlinks are skipped exactly as `create_zip` skips them: a link can point
    outside the source root and bypass name-based exclusion.
    """
    matcher = _build_exclusion_matcher(set(excludes) | MANDATORY_EXCLUDES)
    pruned_dirs: set[str] = set()
    if plan is not None:
        # T-150: per-file plan eligibility is the EXACT FileDecision for the
        # exact relative path -- Asset.PNG and asset.png can carry opposite
        # include/exclude verdicts and neither may collapse into the other.
        # Directory pruning keeps its normalized (case-insensitive) contract.
        pruned_dirs = set(plan.pruned_dirs_rel)
    if source.is_file():
        if not source.is_symlink() and not matcher(source):
            yield source
        return
    for root, dirs, files in os.walk(source):
        base = Path(root)
        # Exclusion is a pure name/path test and is checked FIRST: an excluded
        # directory must not even be stat'ed for the symlink question.
        kept: list[str] = []
        for name in dirs:
            dpath = base / name
            rel_dir = dpath.relative_to(source).as_posix().lower()
            if plan is not None and rel_dir in pruned_dirs:
                continue
            if matcher(dpath) or dpath.is_symlink():
                continue
            kept.append(name)
        dirs[:] = kept
        for name in files:
            path = base / name
            if path.is_symlink():
                continue
            if matcher(path):
                continue
            if plan is not None:
                decision = plan.decision_for(path.relative_to(source).as_posix())
                if decision is not None and not decision.include:
                    continue
            yield path



def create_zip(
    source_dir: str | Path,
    output_zip: Path,
    excludes: set[str],
    cancel_event: Optional[threading.Event] = None,
    log_callback: Optional[Callable[[str], None]] = None,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
    manifest_meta: Optional[dict] = None,
    plan: Optional[FidelityPlan] = None,
) -> ZipStats:
    """
    Creates a ZIP archive from source_dir into output_zip using a .part temporary file.

    T-147: when ``plan`` is supplied its per-file decisions are authoritative
    (safety policy, overrides, configured excludes, media sampling, size trim).
    Without a plan the legacy matcher applies and the manifest honestly reports
    FULL semantics -- everything the explicit policy allowed was included.
    """
    source = Path(source_dir).resolve()
    if not source.exists():
        raise FileNotFoundError(f"Source not found: {source}")

    is_file = source.is_file()
    output_zip.parent.mkdir(parents=True, exist_ok=True)
    # CORE-005: every pack owns a collision-resistant staging file so concurrent
    # same-stem packs in the same second cannot clobber each other's .part.
    part_path = output_zip.with_name(f"{output_zip.name}.part.{uuid.uuid4().hex}")
    if part_path.exists():
        try:
            part_path.unlink()
        except OSError:
            pass

    # Enforce mandatory excludes regardless of user config.
    excludes = set(excludes) | MANDATORY_EXCLUDES
    normalized_excludes = _build_exclusion_matcher(excludes)

    log = log_callback or (lambda msg: None)
    prog = progress_callback or (lambda added, b_written, cur_path: None)
    c_event = cancel_event or threading.Event()

    stats = ZipStats()
    if plan is not None:
        stats.files_discovered = plan.discovered
        stats.files_included = plan.included
        stats.files_excluded = plan.excluded
        stats.files_failed = plan.failed
        stats.source_bytes = plan.source_bytes
        stats.excluded_bytes = plan.excluded_bytes
        stats.unknown_size_entries = plan.unknown_size_entries

    def on_walk_error(err):
        stats.walk_errors += 1
        log(f"! unreadable directory skipped: {err.filename}: {err}")

    try:
        with zipfile.ZipFile(part_path, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
            if is_file:
                # Single file packing
                if c_event.is_set():
                    raise PackingCancelled("Cancelled by user")
                # CORE-006: never package a symlink target; the link escapes the
                # chosen source and can pull in arbitrary external data.
                if source.is_symlink():
                    raise ValueError(f"Refusing to package symlink source: {source}")
                # CORE-003 (audit/3.md): the mandatory-secret boundary existed
                # only for directory traversal. Refused loudly rather than packed
                # empty: an empty successful archive is a worse answer than an error.
                if _path_is_excluded_normalized(source, normalized_excludes):
                    raise ValueError(
                        f"Refusing to package an excluded file: {source.name} matches an exclusion rule"
                    )
                try:
                    size = source.stat().st_size
                except OSError:
                    size = 0
                stats.files_discovered = 1
                stats.files_included = 1
                stats.source_bytes = size
                arcname = source.name
                zinfo = zipfile.ZipInfo.from_file(source, arcname, strict_timestamps=False)
                zinfo.compress_type = compress_type_for(arcname)
                with open(source, "rb") as src, zf.open(zinfo, "w") as dst:
                    while True:
                        if c_event.is_set():
                            raise PackingCancelled("Cancelled by user")
                        chunk = src.read(1024 * 1024)
                        if not chunk:
                            break
                        dst.write(chunk)
                        stats.bytes_written += len(chunk)
                        stats.included_bytes += len(chunk)
                # The bytes read are the truth: a file that grew between the stat
                # and the read must not leave source_bytes disagreeing with it.
                stats.source_bytes = stats.included_bytes
                stats.files_added = 1
                prog(1, stats.bytes_written, str(source))
            else:
                for root, dirs, files in os.walk(source, onerror=on_walk_error):
                    if c_event.is_set():
                        raise PackingCancelled("Cancelled by user")

                    kept_dirs: list[str] = []
                    for d in dirs:
                        dpath = Path(root) / d
                        # PERF-004: matcher BEFORE is_symlink() -- the symlink
                        # probe is a stat, and an excluded directory must never
                        # be stat'ed. Same outcome either order: an excluded
                        # symlink dir is skipped, a non-excluded symlink dir is
                        # skipped by CORE-006.
                        if plan is not None:
                            rel_dir = dpath.relative_to(source).as_posix().lower()
                            if rel_dir in plan.pruned_dirs_rel:
                                continue
                        if _path_is_excluded_normalized(dpath, normalized_excludes):
                            continue
                        if dpath.is_symlink():
                            continue
                        kept_dirs.append(d)
                    dirs[:] = kept_dirs

                    for filename in files:
                        if c_event.is_set():
                            raise PackingCancelled("Cancelled by user")

                        file_path = Path(root) / filename
                        # CORE-006: skip filesystem links; they can point outside
                        # the source root and bypass name/path exclusion rules.
                        if file_path.is_symlink():
                            if plan is None:
                                stats.files_discovered += 1
                                stats.files_excluded += 1
                                log(f"! symlink skipped (link target excluded): {file_path}")
                            continue

                        # T-150: identity is the exact source-relative POSIX
                        # path; the packer must tell Asset.PNG from asset.png.
                        rel = file_path.relative_to(source).as_posix()
                        planned_size = 0
                        if plan is not None:
                            decision = plan.decision_for(rel)
                            if decision is None:
                                # The plan never recorded this file (a transient
                                # traversal/stat error during planning). It must
                                # NEVER silently disappear: count it failed so
                                # the pack reports partial and the manifest is
                                # truthful. (discovered keeps the invariant.)
                                # CORE-003: its bytes are unaccounted for on both
                                # sides, so they enter source_bytes and
                                # failed_bytes together -- or, if unreadable, are
                                # declared unknown rather than assumed zero.
                                stats.files_discovered += 1
                                stats.files_failed += 1
                                try:
                                    missed = file_path.stat().st_size
                                    stats.source_bytes += missed
                                    stats.failed_bytes += missed
                                except OSError:
                                    stats.unknown_size_entries += 1
                                log(f"! unplanned file (plan walk missed it): {file_path}")
                                continue
                            if not decision.include:
                                continue  # already accounted in plan totals
                            planned_size = decision.size
                        else:
                            stats.files_discovered += 1
                            try:
                                size = file_path.stat().st_size
                            except OSError:
                                # An unreadable size is declared unknown, never
                                # counted as a known zero.
                                size = 0
                                stats.unknown_size_entries += 1
                            stats.source_bytes += size
                            if _path_is_excluded_normalized(file_path, normalized_excludes):
                                stats.files_excluded += 1
                                stats.excluded_bytes += size
                                continue
                            planned_size = size

                        included_before = stats.included_bytes
                        try:
                            arcname = file_path.relative_to(source)
                            zinfo = zipfile.ZipInfo.from_file(
                                file_path, str(arcname), strict_timestamps=False
                            )
                            zinfo.compress_type = compress_type_for(arcname.name)
                            with open(file_path, "rb") as src, zf.open(zinfo, "w") as dst:
                                while True:
                                    if c_event.is_set():
                                        raise PackingCancelled("Cancelled by user")
                                    chunk = src.read(1024 * 1024)
                                    if not chunk:
                                        break
                                    dst.write(chunk)
                                    stats.bytes_written += len(chunk)
                                    stats.included_bytes += len(chunk)
                            # A file can change between planning and packing. The
                            # bytes actually read are the truth; source_bytes
                            # absorbs the drift so it keeps describing observed
                            # material instead of a stale plan (CORE-003).
                            drift = (stats.included_bytes - included_before) - planned_size
                            if drift:
                                stats.source_bytes += drift
                            stats.files_added += 1
                            if plan is None:
                                stats.files_included += 1
                            if stats.files_added == 1 or stats.files_added % 50 == 0:
                                prog(stats.files_added, stats.bytes_written, str(file_path))
                        except PackingCancelled:
                            raise
                        except (OSError, ValueError, zipfile.BadZipFile) as exc:
                            # A file that could not be read is neither included
                            # nor excluded: its planned bytes move to the failed
                            # column and any partial read is taken back out of
                            # included_bytes, or the byte identity is unsatisfiable
                            # the moment one file fails.
                            stats.included_bytes = included_before
                            stats.failed_bytes += planned_size
                            stats.files_failed += 1
                            if plan is not None:
                                stats.files_included = max(0, stats.files_included - 1)
                            log(f"! unreadable/locked file skipped: {file_path}: {exc}")

            # Write manifest inside archive if requested
            if manifest_meta is not None:
                manifest_payload = generate_manifest_data(
                    project_name=manifest_meta.get("project_name", source.name),
                    source_path=str(source),
                    source_kind="file" if is_file else "folder",
                    stats=stats,
                    fidelity=_fidelity_payload(plan),
                    extra_meta=manifest_meta.get("extra_meta"),
                )
                manifest_bytes = json.dumps(manifest_payload, ensure_ascii=False, indent=2).encode("utf-8")
                zinfo = zipfile.ZipInfo(MANIFEST_FILENAME)
                zinfo.compress_type = compress_type_for(MANIFEST_FILENAME)
                zf.writestr(zinfo, manifest_bytes)
                stats.files_added += 1
                stats.bytes_written += len(manifest_bytes)

        if c_event.is_set():
            raise PackingCancelled("Cancelled by user")

        part_path.replace(output_zip)
        archive_receipt.invalidate(output_zip)
        return stats
    except Exception:
        if part_path.exists():
            try:
                part_path.unlink()
            except OSError:
                pass
        raise



def verify_zip(output_zip: Path, expected_count: int) -> int:
    """Verifies that the created zip archive is valid and has expected count of entries."""
    if not output_zip.exists():
        raise FileNotFoundError(f"Archive not found: {output_zip}")
    with zipfile.ZipFile(output_zip, "r") as zf:
        names = zf.namelist()
        bad = zf.testzip()
    if bad is not None:
        raise ValueError(f"Corrupt entry in {output_zip.name}: {bad}")
    if len(names) != expected_count:
        raise ValueError(
            f"Entry count mismatch in {output_zip.name}: "
            f"{len(names)} in archive vs {expected_count} expected"
        )
    return len(names)


def read_archive_manifest(archive: Path) -> Optional[dict]:
    """The manifest embedded in ``archive``, or None when unreadable/absent.

    CORE-006 (audit/6.md): reuse used to be decided on mtime alone and never
    opened the archive it was about to hand back. A legacy archive with no
    manifest, a corrupt zip and an unparsable manifest are all reported the same
    way -- None means "this archive cannot state what it is", which the caller
    must treat as a reason to repack rather than a reason to trust it.
    """
    try:
        with zipfile.ZipFile(archive) as zf:
            raw = zf.read(MANIFEST_FILENAME)
    except (OSError, KeyError, ValueError, zipfile.BadZipFile):
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def archive_policy_mismatch(
    archive: Path,
    source: Path,
    expected_fingerprint: str,
    manifest: Optional[dict] = None,
) -> Optional[str]:
    """Why ``archive`` may not be reused for ``source`` under this policy, or None.

    CORE-006 (audit/6.md): packing policy is part of artifact identity. Reproduced
    before this gate: pack COMPACT, make the archive newer than the source, switch
    ``fidelity_profile`` to FULL, and ``ensure_fresh_archive`` returned the COMPACT
    archive as a success -- its own manifest still declaring
    ``fidelity_profile=compact``, ``archive_semantics=audit_representation``.

    Missing or legacy metadata is a mismatch, never a pass: an archive that cannot
    prove which policy built it has to be rebuilt once.

    PERF-001: ``manifest`` lets a caller that already read it (to report the
    reused archive's own policy fields) supply it instead of paying a second zip
    central-directory parse for the same bytes.
    """
    if manifest is None:
        manifest = read_archive_manifest(archive)
    if manifest is None:
        return "archive has no readable AUDAPACK manifest"
    if str(manifest.get("product") or "") != "AUDAPACK":
        return "archive manifest was not written by AUDAPACK"
    try:
        schema = int(manifest.get("schema_version", 0))
    except (TypeError, ValueError):
        return "archive manifest has a non-numeric schema_version"
    # A newer writer may have changed what these keys mean, and an older one may
    # not have recorded the fields this gate reads. Neither is reusable evidence.
    if schema != MANIFEST_SCHEMA_VERSION:
        return f"archive manifest schema {schema} != current {MANIFEST_SCHEMA_VERSION}"
    recorded = str(manifest.get("policy_fingerprint") or "")
    if not recorded:
        return "archive predates policy fingerprinting"
    if recorded != expected_fingerprint:
        return (
            f"packing policy changed (archive {recorded[:12]} != current "
            f"{expected_fingerprint[:12]})"
        )
    # Same policy, different project: the archive is not about this source.
    recorded_source = str(manifest.get("source_path") or "")
    if recorded_source:
        try:
            if Path(recorded_source).resolve() != source.resolve():
                return f"archive was built from a different source ({recorded_source})"
        except OSError:
            return f"archive source path is unresolvable ({recorded_source})"
    return None


def delete_old_archives(output_dir: Path, stem: str, current_zip: Path, log_cb: Optional[Callable[[str], None]] = None) -> tuple[int, int]:
    """Safely deletes previous archives for ``stem`` keeping current_zip.

    Matches both the clean ``{stem}.zip`` form and the legacy ``{stem}_*.zip``
    form (timestamped history) so migration and mixed directories stay clean.
    """
    log = log_cb or (lambda msg: None)
    removed = 0
    remove_errors = 0
    if not output_dir.exists():
        return 0, 0
    try:
        old_candidates = sorted(
            p for p in output_dir.iterdir()
            if p.is_file()
            and p.suffix.lower() == ".zip"
            and archive_belongs_to_stem(p.name, stem)
        )
        for old in old_candidates:
            if old.resolve() == current_zip.resolve():
                continue
            try:
                old.unlink()
                # PERF-004 (audit/12.md): retire the receipt with the archive.
                # Nothing else prunes orphan receipts, so the sidecar store grew
                # by one file per deleted archive forever -- against its stated
                # intent of staying bounded by the archives actually served.
                archive_receipt.invalidate(old)
                removed += 1
                log(f"Removed old archive: {old.name}")
            except Exception as exc:
                remove_errors += 1
                log(f"! Could not remove old archive {old.name}: {exc}")
    except Exception as exc:
        log(f"! Error scanning old archives: {exc}")
    return removed, remove_errors


_ARCHIVE_DIRECTORY_INDEX: dict[str, tuple[int, int, list[tuple[float, str]]]] = {}


def _archive_directory_index(output_dir: Path) -> list[tuple[float, str]]:
    if not output_dir or not output_dir.exists() or not output_dir.is_dir():
        return []
    try:
        stat = output_dir.stat()
        key = str(output_dir.resolve())
        signature = (stat.st_mtime_ns, stat.st_size)
        cached = _ARCHIVE_DIRECTORY_INDEX.get(key)
        if cached and cached[:2] == signature:
            return cached[2]
        candidates: list[tuple[float, str]] = []
        with os.scandir(output_dir) as it:
            for entry in it:
                if entry.is_file() and entry.name.lower().endswith(".zip"):
                    candidates.append((entry.stat().st_mtime, entry.path))
        candidates.sort(key=lambda item: item[0], reverse=True)
        _ARCHIVE_DIRECTORY_INDEX[key] = (signature[0], signature[1], candidates)
        return candidates
    except OSError:
        return []


def find_latest_archive(output_dir: Path, stem: str) -> Optional[Path]:
    """Returns most recently modified ZIP archive for ``stem``."""
    safe_stem = safe_archive_stem(stem)
    for _mtime, path in _archive_directory_index(output_dir):
        if archive_belongs_to_stem(Path(path).name, safe_stem):
            return Path(path)
    return None


def find_archive_for_project(project: "Project", output_dir: Path) -> Optional[Path]:
    """The canonical archive for a project, by explicit identity first.

    CORE-005 (audit/3.md): every alias went into ONE unordered set, and blank
    values were passed through `safe_archive_stem()` -- which maps "" to the
    literal fallback `"Archive"`. Two consequences, both reproduced: a project
    with no `archive_name` matched a stray generic `Archive.zip` in the output
    directory, and a NEWER display-name archive won over the explicitly
    configured `archive_name` because the scan was global newest-first. This
    resolver feeds packing freshness and Bridge artifact ownership, so it decided
    those on the wrong ZIP.

    Ordered phases now: the configured `archive_name` family, then the display
    name, then the id. Newest-within-a-family is unchanged -- that is the history
    behaviour timestamped archives depend on. A blank value contributes no alias
    at all, because it is not an identity.
    """
    index = _archive_directory_index(output_dir)
    for raw in (project.archive_name, project.display_name, project.id):
        if not str(raw or "").strip():
            continue
        stem = safe_archive_stem(str(raw))
        if not stem:
            continue
        for _mtime, path in index:
            if archive_belongs_to_stem(Path(path).name, stem):
                return Path(path)
    return None


def project_for_archive_filename(filename: str, projects: Iterable["Project"]) -> Optional["Project"]:
    """The one registered project whose canonical archive ``filename`` is.

    Same identity order as :func:`find_archive_for_project`, and the same
    anchored stem match, so a sibling ``{stem}_Bar`` never claims ``{stem}``.
    A name two projects could own is ambiguous and answers None.
    """
    name = Path(str(filename or "")).name
    if not name.lower().endswith(".zip"):
        return None
    owners: list["Project"] = []
    for project in projects:
        for raw in (project.archive_name, project.display_name, project.id):
            if str(raw or "").strip() and archive_belongs_to_stem(name, safe_archive_stem(str(raw))):
                owners.append(project)
                break
    return owners[0] if len(owners) == 1 else None


def resolve_output_dir(
    source_path: str | Path,
    packing: PackingConfig,
    fallback: Path,
    group: Optional[str] = None,
    project: Optional[Project] = None,
) -> Path:
    """Return the directory where the archive for ``source_path`` should be written.

    The directory is chosen by ``packing.output_layout``:

    - ``single_folder`` (legacy): every archive goes to ``packing.output_dir``
      if set, otherwise to ``fallback`` (the app runtime dir). All projects
      share the same output directory.

    - ``alongside_projects``: the archive is written as a SIBLING of the
      project folder, i.e. to ``source_path.parent``. The archive is NEVER
      written inside the project (W2-003 self-referential guard): a project
      at ``V:\\code\\_PY\\_FastPrompter`` produces
      ``V:\\code\\_PY\\_FastPrompter.zip`` next to the folder. This is the
      user's "archive next to the project" layout.

    - ``grouped_by_priority``: the archive is written into group subfolders
      (e.g. ``MAIN0/``, ``SIDE0/``) under the output root, separate from text audits.
    """
    layout = normalize_output_layout(getattr(packing, "output_layout", DEFAULT_OUTPUT_LAYOUT))
    if layout == OUTPUT_LAYOUT_ALONGSIDE_PROJECTS:
        sp = Path(source_path)
        try:
            parent = sp.parent
        except Exception:
            parent = None
        if parent is not None and str(parent) not in ("", ".", "/"):
            # Reject the drive-root case ("V:\\" parent is "V:\\") because the
            # archive would land at the drive root and the W2-003 self-pack
            # guard would reject it anyway. Fall back to single_folder in that
            # edge case so the pack still succeeds.
            if str(parent) != str(sp):
                return parent

    if layout == OUTPUT_LAYOUT_GROUPED_BY_PRIORITY:
        grp = (group or (project.priority_group if project else None) or "MAIN0").strip().upper()
        out = (packing.output_dir or "").strip()
        base = Path(out) if out else Path(fallback)
        # Avoid dumping archives directly inside text audit wave folders if base matches audit root
        try:
            from audapack.config import DEFAULT_AUDIT_ROOT
            if base.resolve() == Path(DEFAULT_AUDIT_ROOT).resolve():
                base = base / "_ARCHIVES"
        except Exception:
            pass
        return base / grp

    # single_folder (default + fallback)
    out = (packing.output_dir or "").strip()
    if out:
        return Path(out)
    return Path(fallback)


def pack_single(
    source_path: str | Path,
    output_dir: Path,
    archive_stem: str,
    excludes: set[str],
    delete_old: bool = True,
    include_timestamp: Optional[bool] = None,
    cancel_event: Optional[threading.Event] = None,
    log_callback: Optional[Callable[[str], None]] = None,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
    manifest_meta: Optional[dict] = None,
    packing: Optional[PackingConfig] = None,
    prebuilt_plan: Optional[FidelityPlan] = None,
) -> PackResult:
    """Pack a single folder or file into a timestamped ZIP in output_dir.

    T-147: when ``packing`` is supplied its fidelity profile, soft budget,
    media sampling and always_include/always_exclude overrides are honored;
    the returned :class:`PackResult` and the in-archive manifest then carry
    truthful accounting (discovered/included/excluded/failed + profile +
    archive semantics). Without ``packing`` the legacy behaviour applies and
    the manifest honestly reports FULL semantics.

    T-155 (PERF-004 residue): ``prebuilt_plan`` lets the freshness probe hand
    its complete plan straight to the packer so a stale verdict never re-walks
    the tree. The caller owns the safety contract: the plan must have been
    built for the same source under the same policy, fully walked, with its
    census finalized (``finalize_pruned_census``) -- a prefix verdict or a
    cancelled plan must never arrive here.
    """
    log = log_callback or (lambda msg: None)
    source = Path(source_path)
    stem = safe_archive_stem(archive_stem)
    use_ts = include_timestamp if include_timestamp is not None else (not delete_old)
    if use_ts:
        run_stamp = datetime.now().strftime("%d.%m.%y-T%H-%M-%S")
        output_path = output_dir / f"{stem}_{run_stamp}.zip"
        suffix = 1
        while output_path.exists():
            output_path = output_dir / f"{stem}_{run_stamp}-{suffix}.zip"
            suffix += 1
    else:
        output_path = output_dir / f"{stem}.zip"

    # W2-003: reject a self-referential topology. Packing into the source (or a
    # descendant of it) makes the operation consume its own staging/output area.
    if source.exists():
        source_res = source.resolve()
        output_res = Path(output_dir).resolve()
        if source_res.is_dir() and (output_res == source_res or output_res.is_relative_to(source_res)):
            return PackResult(
                project_id=stem,
                name=stem,
                source_path=str(source),
                success=False,
                error_message=f"Output directory is inside the source ({output_res}); refusing self-referential pack.",
            )

    if not source.exists():
        return PackResult(
            project_id=stem,
            name=stem,
            source_path=str(source),
            success=False,
            error_message=f"Source does not exist: {source}",
        )

    # SAIPEN audit-manifest authority gate (SRC-007 / T-850). A project that
    # carries `.saipen/` is protocol evidence, and the 2026-09-15 15:11:04
    # SAIPENVIEW archive proved the old ordering ships a green package over a
    # red snapshot: the inventory computed the truthful verdict
    # (PROTOCOL_MANIFEST_ABSENT, authoritative_state=false) and packed it
    # anyway, because nothing in the canonical path ever ran
    # `saipen audit manifest --write`. The gate makes the normal pack path
    # guarantee ONE of: (1) a current contract manifest exists now --
    # generated through the REAL protocol CLI when missing, never synthesized
    # here -- or (2) a fail-closed refusal BEFORE any archive byte exists.
    # Non-SAIPEN projects (no `.saipen/`) pass through untouched.
    gate = saipen_manifest_gate.prepare(
        source,
        cancel_event=cancel_event,
        log_callback=log,
    )
    if not gate.ok and gate.code != saipen_evidence.STATUS_NOT_SAIPEN:
        if gate.code == "PACK_CANCELLED":
            message = "Cancelled by user"
        elif gate.code == saipen_evidence.STATUS_CONTRACT_UNKNOWN:
            # TARGET G: the manifest is present and current; only its contract
            # VERSION is beyond this collector. "Run saipen audit manifest
            # --write" would be misleading advice that cannot repair anything
            # (the installed protocol writes that same newer contract), so the
            # gate's own detail -- which names the real remedy -- is surfaced
            # alone, with no regeneration instruction appended.
            message = (
                f"SAIPEN audit-manifest precondition failed ({gate.code}): "
                f"{gate.detail} No archive was produced."
            )
        else:
            message = (
                f"SAIPEN audit-manifest precondition failed ({gate.code}): "
                f"{gate.detail}. Run `saipen audit manifest --write` in the "
                "project (or repair the owning SAIPEN protocol component) "
                "and repack; no archive was produced."
            )
        log(f"FAIL {stem}: {message}")
        return PackResult(
            project_id=stem,
            name=stem,
            source_path=str(source),
            success=False,
            status=PACK_STATUS_FAILED_INVENTORY,
            error_code=gate.code,
            error_message=message,
        )

    # T-190 (SRC-046): freeze the source inventory BEFORE the transaction
    # opens. Membership comes only from this frozen set -- Git worktrees
    # inventory as tracked_existing UNION untracked_nonignored via bounded Git
    # commands; genuinely non-Git sources keep the bounded walk fallback under
    # the same frozen/fail-closed contract. An unreliable inventory fails the
    # pack here, before a single archive byte exists.
    # T-246: the reserved control paths THIS run regenerates. A tracked
    # AUDAPACK-generated artifact on one of them is superseded by the fresh
    # one instead of failing the pack; a project-owned file on a reserved name
    # still fails closed, and nothing here widens the hard-safety policy.
    supersedable_reserved = frozenset(RESERVED_ARCHIVE_NAMES)
    if manifest_meta is None:
        supersedable_reserved = frozenset({INVENTORY_MANIFEST_PATH})

    try:
        inventory, plan = build_pack_inventory(
            source,
            excludes,
            packing=packing,
            prebuilt_plan=prebuilt_plan,
            supersedable_reserved=supersedable_reserved,
            cancel_event=cancel_event,
        )
    except SourceInventoryError as exc:
        log(f"FAIL {stem}: {exc}")
        return PackResult(
            project_id=stem,
            name=stem,
            source_path=str(source),
            success=False,
            status=PACK_STATUS_FAILED_INVENTORY,
            # The stage publishes the exact rule that refused (T-190 codes);
            # carrying it as data keeps the Project Room classification exact
            # instead of re-derived by parsing the message.
            error_code=exc.code,
            error_message=str(exc),
            first_error_path=exc.rel,
        )
    if inventory.mode == "git":
        fidelity_payload = _fidelity_payload_for_inventory(inventory, packing, excludes)
        profile_name = fidelity_payload["fidelity_profile"]
        archive_sem = fidelity_payload["archive_semantics"]
    else:
        fidelity_payload = _fidelity_payload(plan)
        profile_name = plan.profile if plan else ""
        archive_sem = plan.archive_semantics if plan else ""

    # CORE-001: serialize the entire same-target transaction (filename
    # selection, backup, archive creation + atomic replace, verification,
    # retention, backup cleanup, and rollback) under a cross-process lock
    # keyed by the resolved output directory + output stem. Reusing the
    # existing cross-process locking primitive ensures all processes sharing
    # this state dir coordinate on the same owner, and rollback ownership
    # stays local to the transaction so a failure can never unlink or
    # restore over output produced by another successful transaction.
    tx_lock_path = _pack_transaction_lock_path(output_dir, stem)
    try:
        with cross_process_lock(tx_lock_path):
            # Re-evaluate timestamp collision under the lock so two packs
            # inside the same second never pick the same numeric suffix
            # loop and overwrite each other.
            if use_ts:
                suffix = 1
                while output_path.exists():
                    output_path = output_dir / f"{stem}_{run_stamp}-{suffix}.zip"
                    suffix += 1

            # W2-001: back up any existing complete archive before overwriting so a
            # partial/failed run can never destroy the last good backup. The diagnostic
            # name uses a dot (not underscore) after the stem so retention globbing
            # (`{stem}_*`) never touches it.
            #
            # CORE-001 (audit/1.md): a failure HERE used to be swallowed
            # (`backup_path = None`) and the pack carried on. create_zip then
            # atomically replaced the canonical archive, and a later verify
            # failure unlinked the replacement -- with no backup to restore
            # from, the operator's last good archive was simply gone. Rollback
            # authority is a PRECONDITION for replacing the canonical path: no
            # backup, no replacement.
            backup_path = None
            if delete_old and output_path.exists():
                backup_path = output_path.with_name(f"{stem}.bak.{uuid.uuid4().hex}.zip")
                backup_error: Optional[OSError] = None
                for attempt in range(_BACKUP_ESTABLISH_ATTEMPTS):
                    try:
                        output_path.replace(backup_path)
                        backup_error = None
                        break
                    except OSError as exc:
                        backup_error = exc
                        if attempt + 1 < _BACKUP_ESTABLISH_ATTEMPTS:
                            time.sleep(0.02 * (2 ** attempt))
                if backup_error is not None:
                    log(f"FAIL {stem}: cannot secure the previous archive: {backup_error}")
                    return PackResult(
                        project_id=stem,
                        name=stem,
                        source_path=str(source),
                        output_path=output_path if output_path.exists() else None,
                        success=False,
                        error_message=(
                            f"Could not move the existing archive aside ({backup_error}); "
                            "refusing to repack because a failure would have nothing to restore. "
                            "The previous archive is untouched."
                        ),
                    )

            try:
                output_dir.mkdir(parents=True, exist_ok=True)

                def _refreeze_inventory():
                    """One bounded re-freeze after a mid-pack source change."""
                    nonlocal inventory, plan, fidelity_payload
                    inventory, plan = build_pack_inventory(
                        source,
                        excludes,
                        packing=packing,
                        prebuilt_plan=None,
                        supersedable_reserved=supersedable_reserved,
                        cancel_event=cancel_event,
                    )
                    if inventory.mode == "git":
                        fidelity_payload = _fidelity_payload_for_inventory(
                            inventory, packing, excludes
                        )
                    else:
                        fidelity_payload = _fidelity_payload(plan)

                # T-190: WRITE -> VERIFY -> COMMIT from the frozen inventory.
                # A source that changes under the writer earns exactly one
                # full re-freeze + rewrite retry; anything worse fails the
                # pack with no newly-created final archive.
                stats = None
                for attempt in range(1, _SOURCE_CHANGE_MAX_ATTEMPTS + 1):
                    try:
                        staged = stage_inventory_zip(
                            inventory.source if inventory.source != Path(source).resolve() else source,
                            output_path,
                            inventory,
                            plan=plan,
                            cancel_event=cancel_event,
                            log_callback=log,
                            progress_callback=progress_callback,
                            manifest_meta=manifest_meta,
                            fidelity_payload=fidelity_payload,
                            project_name=manifest_meta.get("project_name") if manifest_meta else None,
                        )
                    except SourceChangedError as exc:
                        if attempt < _SOURCE_CHANGE_MAX_ATTEMPTS:
                            log(
                                f"! {stem}: source changed during pack ({exc.rel}); "
                                "re-freezing the inventory for one bounded retry"
                            )
                            _refreeze_inventory()
                            continue
                        raise
                    try:
                        verify_inventory_archive(staged.part_path, staged.sha256_by_rel)
                    except ArchiveVerifyError:
                        if staged.part_path.exists():
                            try:
                                staged.part_path.unlink()
                            except OSError:
                                pass
                        raise
                    staged.part_path.replace(output_path)
                    archive_receipt.invalidate(output_path)
                    stats = staged.stats
                    break

                added, raw_bytes, skipped, walk_errors = (
                    stats.files_added,
                    stats.bytes_written,
                    stats.files_failed,
                    stats.walk_errors,
                )
                entries = added
                size_bytes = output_path.stat().st_size

                if delete_old:
                    delete_old_archives(output_dir, stem, output_path, log)

                if backup_path and backup_path.exists():
                    try:
                        backup_path.unlink()
                    except OSError:
                        pass

                log(f"OK {output_path.name}: {entries} files, {human_mb(raw_bytes)} -> {human_mb(size_bytes)}")
                for superseded in inventory.superseded_control:
                    log(
                        f"  {superseded}: tracked AUDAPACK-generated control artifact "
                        f"superseded by the one in this archive -- run "
                        f"`git rm --cached {superseded}` to untrack it"
                    )
                return PackResult(
                    project_id=stem,
                    name=stem,
                    source_path=str(source),
                    output_path=output_path,
                    success=True,
                    status=PACK_STATUS_PACKED,
                    files_added=entries,
                    files_included=stats.files_included,
                    raw_bytes=raw_bytes,
                    archive_bytes=size_bytes,
                    skipped_files=skipped,
                    walk_errors=walk_errors,
                    files_discovered=stats.files_discovered,
                    files_excluded=stats.files_excluded,
                    files_failed=stats.files_failed,
                    excluded_bytes=stats.excluded_bytes,
                    fidelity_profile=profile_name,
                    archive_semantics=archive_sem,
                    git_summary=inventory.git_summary() if inventory.mode == "git" else "",
                )
            except (SourceReadError, SourceChangedError, ArchiveVerifyError, SourceInventoryError) as exc:
                # T-190 fail-closed source contract: a read failure, an
                # unexplained source change (after the one retry), a parity
                # failure or a re-freeze failure must never produce a
                # successful final archive. Remove the failed output FIRST,
                # then restore the previous good archive from the backup.
                if output_path.exists():
                    try:
                        output_path.unlink()
                    except OSError:
                        pass
                if backup_path and backup_path.exists():
                    try:
                        backup_path.replace(output_path)
                    except OSError as restore_exc:
                        # Preserve the backup under its diagnostic name; never destroy it.
                        log(f"WARN {stem}: could not restore backup {backup_path.name}: {restore_exc}")
                        return PackResult(
                            project_id=stem,
                            name=stem,
                            source_path=str(source),
                            output_path=backup_path if backup_path.exists() else None,
                            success=False,
                            error_message=f"{exc} (previous archive preserved as {backup_path.name})",
                            first_error_path=getattr(exc, "rel", ""),
                        )
                if isinstance(exc, SourceReadError):
                    status = PACK_STATUS_FAILED_SOURCE_READ
                    message = f"source file could not be read: {exc.rel}: {exc.reason}"
                elif isinstance(exc, SourceChangedError):
                    status = PACK_STATUS_FAILED_SOURCE_CHANGED
                    message = (
                        f"source changed during pack (after one bounded retry): {exc.rel}: {exc.reason}"
                    )
                elif isinstance(exc, ArchiveVerifyError):
                    status = PACK_STATUS_FAILED_VERIFY
                    message = f"archive failed post-write parity verification: {exc.message}"
                else:
                    status = PACK_STATUS_FAILED_INVENTORY
                    message = str(exc)
                log(f"FAIL {stem}: [{status}] {message}")
                return PackResult(
                    project_id=stem,
                    name=stem,
                    source_path=str(source),
                    output_path=output_path if output_path.exists() else None,
                    success=False,
                    status=status,
                    error_code=getattr(exc, "code", ""),
                    error_message=message,
                    first_error_path=getattr(exc, "rel", ""),
                )
            except Exception as exc:
                # Restore previous good archive on failure. The failed new output must
                # be removed FIRST; the backup is the only recovery authority and must
                # never be unlinked just because a failed replacement exists.
                if output_path.exists():
                    try:
                        output_path.unlink()
                    except OSError:
                        pass
                if backup_path and backup_path.exists():
                    try:
                        backup_path.replace(output_path)
                    except OSError:
                        # Preserve the backup under its diagnostic name; never destroy it.
                        log(f"WARN {stem}: could not restore backup {backup_path.name}: {exc}")
                        return PackResult(
                            project_id=stem,
                            name=stem,
                            source_path=str(source),
                            output_path=backup_path if backup_path.exists() else None,
                            success=False,
                            error_message=f"{exc} (previous archive preserved as {backup_path.name})",
                        )
                log(f"FAIL {stem}: {exc}")
                return PackResult(
                    project_id=stem,
                    name=stem,
                    source_path=str(source),
                    success=False,
                    error_message=str(exc),
                )
    except TimeoutError:
        log(f"FAIL {stem}: pack transaction lock busy: {tx_lock_path}")
        return PackResult(
            project_id=stem,
            name=stem,
            source_path=str(source),
            success=False,
            error_message=f"Pack transaction for '{stem}' is already in progress; retry shortly",
        )
