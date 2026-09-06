"""Packing engine for AUDAPACK.

Creates clean, verified zip archives with .part staging, exclude filtering,
Zip64 support, and optional manifest generation.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import threading
import time
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

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
    PROFILE_FULL,
    FidelityPlan,
    archive_semantics_for,
    exclude_reason_summary,
    failure_summary,
    pruned_census_totals,
)
from audapack.models import PackResult, Project

MANIFEST_FILENAME = "_AUDAPACK_MANIFEST.json"

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
    skip_rel: set[str] = set()
    pruned_dirs: set[str] = set()
    if plan is not None:
        skip_rel = {
            rel for rel, d in plan.decisions.items() if not d.include
        } | {rel.lower() for rel in plan.extra_excluded_rel}
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
            rel_lower = path.relative_to(source).as_posix().lower()
            if plan is not None and rel_lower in skip_rel:
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

                        rel_lower = file_path.relative_to(source).as_posix().lower()
                        planned_size = 0
                        if plan is not None:
                            decision = plan.decisions.get(rel_lower)
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
) -> PackResult:
    """Pack a single folder or file into a timestamped ZIP in output_dir.

    T-147: when ``packing`` is supplied its fidelity profile, soft budget,
    media sampling and always_include/always_exclude overrides are honored;
    the returned :class:`PackResult` and the in-archive manifest then carry
    truthful accounting (discovered/included/excluded/failed + profile +
    archive semantics). Without ``packing`` the legacy behaviour applies and
    the manifest honestly reports FULL semantics.
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

    # T-147: build the fidelity plan after the topology/existence guards so
    # error paths never pay for a full tree walk. The plan is the single
    # source of truth for both the zipper and the freshness walk.
    plan = None
    if packing is not None:
        from audapack.fidelity import build_plan_from_config
        plan = build_plan_from_config(source, packing, excludes)

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
                stats = create_zip(
                    source,
                    output_path,
                    excludes,
                    cancel_event=cancel_event,
                    log_callback=log,
                    progress_callback=progress_callback,
                    manifest_meta=manifest_meta,
                    plan=plan,
                )
                added, raw_bytes, skipped, walk_errors = (
                    stats.files_added,
                    stats.bytes_written,
                    stats.files_failed,  # T-147: skipped_files now means failed
                    stats.walk_errors,
                )
                entries = verify_zip(output_path, added)
                size_bytes = output_path.stat().st_size

                # W2-001: a partial archive (skipped files or traversal errors) must not
                # be reported as a complete, successful pack, and must never delete the
                # previous good archive.
                # T-147: excluded is INTENTIONAL (configured ignores, media
                # sampling, size trim) and must not mark the pack partial.
                # Only unreadable/locked files and traversal errors do.
                partial = stats.files_failed > 0 or stats.walk_errors > 0
                if partial:
                    diag = output_path.with_name(f"{stem}.PARTIAL.{uuid.uuid4().hex}.zip")
                    try:
                        output_path.replace(diag)
                    except OSError:
                        pass
                    if backup_path and backup_path.exists():
                        try:
                            backup_path.replace(output_path)  # restore previous good
                        except OSError:
                            pass
                    return PackResult(
                        project_id=stem,
                        name=stem,
                        source_path=str(source),
                        output_path=diag if diag.exists() else output_path,
                        success=False,
                        error_message=(
                            f"Partial archive: {skipped} file(s) skipped, {walk_errors} walk error(s). "
                            f"Previous complete archive preserved."
                        ),
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
                        fidelity_profile=plan.profile if plan else "",
                        archive_semantics=plan.archive_semantics if plan else "",
                    )

                if delete_old:
                    delete_old_archives(output_dir, stem, output_path, log)

                if backup_path and backup_path.exists():
                    try:
                        backup_path.unlink()
                    except OSError:
                        pass

                log(f"OK {output_path.name}: {entries} files, {human_mb(raw_bytes)} -> {human_mb(size_bytes)}")
                return PackResult(
                    project_id=stem,
                    name=stem,
                    source_path=str(source),
                    output_path=output_path,
                    success=True,
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
                    fidelity_profile=plan.profile if plan else "",
                    archive_semantics=plan.archive_semantics if plan else "",
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
