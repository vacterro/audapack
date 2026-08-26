"""Packing engine for AUDAPACK.

Creates clean, verified zip archives with .part staging, exclude filtering,
Zip64 support, and optional manifest generation.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import threading
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from audapack.models import PackResult, Project

MANIFEST_FILENAME = "_AUDAPACK_MANIFEST.json"

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


class PackingCancelled(Exception):
    pass


# Per-(output_dir, stem) locks so concurrent same-target retention cannot delete
# each other's archives (CORE-005).
_RETENTION_LOCKS: dict[str, threading.Lock] = {}
_RETENTION_LOCKS_GUARD = threading.Lock()


def _retention_lock(key: str) -> threading.Lock:
    with _RETENTION_LOCKS_GUARD:
        lock = _RETENTION_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _RETENTION_LOCKS[key] = lock
        return lock


def safe_archive_stem(name: str) -> str:
    name = name.strip()
    name = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "_", name)
    name = name.rstrip(" .")
    return name or "Archive"


def human_mb(value: int) -> str:
    return f"{value / (1024 * 1024):.1f} MB"


def path_is_excluded(path: Path | str, patterns: set[str]) -> bool:
    p = path if isinstance(path, Path) else Path(path)
    name = p.name.lower()
    parts: Optional[list[str]] = None
    for pattern in patterns:
        pat = pattern.lower()
        if name == pat or fnmatch.fnmatchcase(name, pat):
            return True
        if parts is None:
            parts = [part.lower() for part in p.parts]
        for part_lower in parts:
            if part_lower == pat or fnmatch.fnmatchcase(part_lower, pat):
                return True
    return False


def generate_manifest_data(
    project_name: str,
    source_path: str,
    source_kind: str,
    files_added: int,
    files_skipped: int,
    extra_meta: Optional[dict] = None,
) -> dict:
    meta = {
        "schema_version": 1,
        "product": "AUDAPACK",
        "created_at": datetime.now().isoformat(),
        "project": project_name,
        "source_path": str(source_path),
        "source_kind": source_kind,
        "files_added": files_added,
        "files_skipped": files_skipped,
    }
    if extra_meta:
        meta.update(extra_meta)
    return meta


def create_zip(
    source_dir: str | Path,
    output_zip: Path,
    excludes: set[str],
    cancel_event: Optional[threading.Event] = None,
    log_callback: Optional[Callable[[str], None]] = None,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
    manifest_meta: Optional[dict] = None,
) -> tuple[int, int, int, int]:
    """
    Creates a ZIP archive from source_dir into output_zip using a .part temporary file.
    Returns: (files_added, bytes_written, skipped_files, walk_errors)
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

    log = log_callback or (lambda msg: None)
    prog = progress_callback or (lambda added, b_written, cur_path: None)
    c_event = cancel_event or threading.Event()

    files_added = 0
    bytes_written = 0
    skipped = 0
    walk_errors = 0

    def on_walk_error(err):
        nonlocal walk_errors
        walk_errors += 1
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
                arcname = source.name
                zinfo = zipfile.ZipInfo.from_file(source, arcname, strict_timestamps=False)
                zinfo.compress_type = zipfile.ZIP_DEFLATED
                with open(source, "rb") as src, zf.open(zinfo, "w") as dst:
                    while True:
                        if c_event.is_set():
                            raise PackingCancelled("Cancelled by user")
                        chunk = src.read(1024 * 1024)
                        if not chunk:
                            break
                        dst.write(chunk)
                        bytes_written += len(chunk)
                files_added = 1
                prog(1, bytes_written, str(source))
            else:
                for root, dirs, files in os.walk(source, onerror=on_walk_error):
                    if c_event.is_set():
                        raise PackingCancelled("Cancelled by user")

                    dirs[:] = [
                        d for d in dirs
                        if not (Path(root) / d).is_symlink()
                        and not path_is_excluded(Path(root) / d, excludes)
                    ]

                    for filename in files:
                        if c_event.is_set():
                            raise PackingCancelled("Cancelled by user")

                        file_path = Path(root) / filename
                        # CORE-006: skip filesystem links; they can point outside
                        # the source root and bypass name/path exclusion rules.
                        if file_path.is_symlink():
                            skipped += 1
                            log(f"! symlink skipped (link target excluded): {file_path}")
                            continue
                        if path_is_excluded(file_path, excludes):
                            continue

                        try:
                            arcname = file_path.relative_to(source)
                            zinfo = zipfile.ZipInfo.from_file(
                                file_path, str(arcname), strict_timestamps=False
                            )
                            zinfo.compress_type = zipfile.ZIP_DEFLATED
                            with open(file_path, "rb") as src, zf.open(zinfo, "w") as dst:
                                while True:
                                    if c_event.is_set():
                                        raise PackingCancelled("Cancelled by user")
                                    chunk = src.read(1024 * 1024)
                                    if not chunk:
                                        break
                                    dst.write(chunk)
                                    bytes_written += len(chunk)
                            files_added += 1
                            if files_added == 1 or files_added % 50 == 0:
                                prog(files_added, bytes_written, str(file_path))
                        except PackingCancelled:
                            raise
                        except (OSError, ValueError, zipfile.BadZipFile) as exc:
                            skipped += 1
                            log(f"! unreadable/locked file skipped: {file_path}: {exc}")

            # Write manifest inside archive if requested
            if manifest_meta is not None:
                manifest_payload = generate_manifest_data(
                    project_name=manifest_meta.get("project_name", source.name),
                    source_path=str(source),
                    source_kind="file" if is_file else "folder",
                    files_added=files_added,
                    files_skipped=skipped,
                    extra_meta=manifest_meta.get("extra_meta"),
                )
                manifest_bytes = json.dumps(manifest_payload, ensure_ascii=False, indent=2).encode("utf-8")
                zinfo = zipfile.ZipInfo(MANIFEST_FILENAME)
                zinfo.compress_type = zipfile.ZIP_DEFLATED
                zf.writestr(zinfo, manifest_bytes)
                files_added += 1

        if c_event.is_set():
            raise PackingCancelled("Cancelled by user")

        part_path.replace(output_zip)
        return files_added, bytes_written, skipped, walk_errors
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
            and (p.name == f"{stem}.zip" or p.name.startswith(f"{stem}_"))
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


def find_latest_archive(output_dir: Path, stem: str) -> Optional[Path]:
    """Returns the most recently modified ZIP archive for ``stem``.

    Matches the clean ``{stem}.zip`` form and the legacy ``{stem}_*.zip``
    timestamped form (mirrors ``pack_single`` behaviour). Returns ``None`` if no
    archive exists or the output directory is missing.
    """
    if not output_dir or not output_dir.exists() or not output_dir.is_dir():
        return None
    safe_stem = safe_archive_stem(stem)
    candidates: list[tuple[float, str]] = []
    clean_name = f"{safe_stem}.zip"
    prefix_name = f"{safe_stem}_"
    try:
        with os.scandir(output_dir) as it:
            for entry in it:
                if entry.is_file() and entry.name.lower().endswith(".zip"):
                    name = entry.name
                    if name == clean_name or name.startswith(prefix_name):
                        candidates.append((entry.stat().st_mtime, entry.path))
    except OSError:
        return None
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return Path(candidates[0][1])


def find_archive_for_project(project: "Project", output_dir: Path) -> Optional[Path]:
    """Resolve the latest archive for a registered project (by archive_name / display_name / id)."""
    candidates = [
        project.archive_name,
        project.display_name,
        project.id,
    ]
    seen: set[str] = set()
    for stem in candidates:
        s = safe_archive_stem(stem or "")
        if not s or s in seen:
            continue
        seen.add(s)
        latest = find_latest_archive(output_dir, s)
        if latest:
            return latest
    return None


def pack_single(
    source_path: str | Path,
    output_dir: Path,
    archive_stem: str,
    excludes: set[str],
    delete_old: bool = True,
    cancel_event: Optional[threading.Event] = None,
    log_callback: Optional[Callable[[str], None]] = None,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
    manifest_meta: Optional[dict] = None,
) -> PackResult:
    """Pack a single folder or file into a timestamped ZIP in output_dir."""
    log = log_callback or (lambda msg: None)
    source = Path(source_path)
    stem = safe_archive_stem(archive_stem)
    # Clean, exact name when old archives are auto-deleted (default): the
    # archive is named exactly after the project, no timestamp garbage. Keep
    # the timestamp only when history is preserved (delete_old=False).
    if delete_old:
        output_path = output_dir / f"{stem}.zip"
    else:
        run_stamp = datetime.now().strftime("%d-%m-%Y-T%H-%M-%S")
        output_path = output_dir / f"{stem}_{run_stamp}.zip"

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

    # W2-001: back up any existing complete archive before overwriting so a
    # partial/failed run can never destroy the last good backup. The diagnostic
    # name uses a dot (not underscore) after the stem so retention globbing
    # (`{stem}_*`) never touches it.
    backup_path = None
    if delete_old and output_path.exists():
        backup_path = output_path.with_name(f"{stem}.bak.{uuid.uuid4().hex}.zip")
        try:
            output_path.replace(backup_path)
        except OSError:
            backup_path = None

    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        added, raw_bytes, skipped, walk_errors = create_zip(
            source,
            output_path,
            excludes,
            cancel_event=cancel_event,
            log_callback=log,
            progress_callback=progress_callback,
            manifest_meta=manifest_meta,
        )
        entries = verify_zip(output_path, added)
        size_bytes = output_path.stat().st_size

        # W2-001: a partial archive (skipped files or traversal errors) must not
        # be reported as a complete, successful pack, and must never delete the
        # previous good archive.
        partial = skipped > 0 or walk_errors > 0
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
                raw_bytes=raw_bytes,
                archive_bytes=size_bytes,
                skipped_files=skipped,
                walk_errors=walk_errors,
            )

        if delete_old:
            # CORE-005: serialize retention for the same target so concurrent
            # packs cannot delete each other's freshly written archives.
            retention_key = f"{Path(output_dir).resolve()}:{stem}"
            with _retention_lock(retention_key):
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
            raw_bytes=raw_bytes,
            archive_bytes=size_bytes,
            skipped_files=skipped,
            walk_errors=walk_errors,
        )
    except Exception as exc:
        # Restore previous good archive on failure (do not leave a broken/empty file).
        if backup_path and backup_path.exists() and not output_path.exists():
            try:
                backup_path.replace(output_path)
            except OSError:
                pass
        elif backup_path and backup_path.exists():
            try:
                backup_path.unlink()
            except OSError:
                pass
        if output_path.exists():
            try:
                output_path.unlink()
            except OSError:
                pass
        log(f"FAIL {stem}: {exc}")
        return PackResult(
            project_id=stem,
            name=stem,
            source_path=str(source),
            success=False,
            error_message=str(exc),
        )
