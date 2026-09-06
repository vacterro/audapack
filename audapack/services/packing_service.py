"""Packing operations — thin wrapper over the existing engine."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from audapack.config import AppConfig, app_dir, load_config
from audapack.fidelity import build_plan_from_config, policy_fingerprint_from_config
from audapack.models import PackResult
from audapack.packing import (
    archive_policy_mismatch,
    find_archive_for_project,
    pack_single,
    read_archive_manifest,
    resolve_output_dir,
)
from audapack.projects import ProjectRegistry
from audapack.saipen import get_saipen_info


class PackingService:
    def __init__(self, config: Optional[AppConfig] = None, base_dir=None):
        self.base_dir = base_dir
        self.config = config or load_config(base_dir)
        self.registry = ProjectRegistry(self.config, base_dir=base_dir, transactional=True)

    def pack_project(
        self,
        project_id: str,
        *,
        progress_callback: Optional[Callable] = None,
        cancel_event=None,
        log_callback: Optional[Callable[[str], None]] = None,
    ):
        proj = self.registry.get_project_by_id(project_id)
        if not proj or not proj.source_path:
            from audapack.models import PackResult
            return PackResult(project_id=project_id, name=project_id, source_path="", success=False, error_message="No source path")

        output_dir = resolve_output_dir(proj.source_path, self.config.packing, fallback=app_dir(), group=proj.priority_group, project=proj)
        excludes = set(self.config.packing.excludes)
        extra_meta = {}
        if self.config.packing.manifest_enabled and proj.source_path:
            info = get_saipen_info(proj.source_path)
            extra_meta["saipen_detected"] = info.detected
            if info.detected:
                extra_meta["git"] = {"branch": info.git_branch, "head": info.git_head, "dirty": info.git_dirty, "changed_files": info.git_changed_files}
        return pack_single(
            source_path=proj.source_path,
            output_dir=output_dir,
            archive_stem=proj.archive_name or proj.display_name,
            excludes=excludes,
            delete_old=self.config.packing.delete_old,
            include_timestamp=getattr(self.config.packing, "include_timestamp", True),
            cancel_event=cancel_event,
            log_callback=log_callback,
            progress_callback=progress_callback,
            manifest_meta={"project_name": proj.display_name, "extra_meta": extra_meta} if self.config.packing.manifest_enabled else None,
            packing=self.config.packing,
        )

    def ensure_fresh_archive(self, project_id: str, *, cancel_event=None, log_callback=None):
        """Return a current archive, packing only when the source is newer.

        The returned path is either an existing archive proven to belong to the
        registered project/output directory or the output of a successful
        ``PackResult``. Failed/partial packs never become trusted artifacts.
        """
        proj = self.registry.get_project_by_id(project_id)
        if not proj or not proj.enabled or not proj.source_path:
            return PackResult(project_id=project_id, name=getattr(proj, "display_name", project_id), source_path="", success=False, error_message="Project is missing, disabled, or has no source path")
        source = Path(proj.source_path)
        if not source.exists():
            return PackResult(project_id=project_id, name=proj.display_name, source_path=str(source), success=False, error_message="Project source path is missing")
        output_dir = resolve_output_dir(source, self.config.packing, fallback=app_dir(), group=proj.priority_group, project=proj)
        existing = find_archive_for_project(proj, output_dir)
        if not (existing and existing.is_file()):
            # Nothing to reuse means nothing to prove: the freshness walk's whole
            # purpose is comparing against an existing archive.
            return self.pack_project(project_id, cancel_event=cancel_event, log_callback=log_callback)
        try:
            archive_mtime = existing.stat().st_mtime
        except OSError:
            return self.pack_project(project_id, cancel_event=cancel_event, log_callback=log_callback)

        # PERF-004 (audit/2.md): only files that can actually enter the archive
        # count, and the first one newer than the archive already settles it.
        # This walked the WHOLE tree, unpruned and unstat-filtered, so reusing an
        # archive traversed exactly the node_modules/.venv/.git/objects weight
        # that packing excludes for performance.
        excludes = set(self.config.packing.excludes)
        # CORE-006 (audit/6.md): packing policy is part of artifact identity, so
        # the policy question is asked FIRST -- it is one archive read, and a
        # mismatch makes any amount of source work obsolete (PERF-001). Reuse used
        # to be a pure timestamp property: packing COMPACT and then switching to
        # FULL handed back the COMPACT archive as a success.
        fingerprint = policy_fingerprint_from_config(self.config.packing, excludes)
        # PERF-001: the manifest is read ONCE. The policy gate and the reused
        # PackResult's own metadata are the same bytes, and parsing the zip
        # central directory twice was the single most expensive step left in a
        # reuse decision on a small tree.
        manifest = read_archive_manifest(existing)
        mismatch = archive_policy_mismatch(existing, source, fingerprint, manifest)
        if mismatch is not None:
            if log_callback:
                log_callback(f"repacking {proj.display_name}: {mismatch}")
            return self.pack_project(project_id, cancel_event=cancel_event, log_callback=log_callback)
        # T-147: the source-root mtime alone settles the common repack case, so
        # it is checked BEFORE the plan build (a full walk) -- a changed root
        # must not pay for a walk the pack is about to repeat anyway.
        try:
            if source.stat().st_mtime > archive_mtime:
                return self.pack_project(project_id, cancel_event=cancel_event, log_callback=log_callback)
        except OSError:
            return self.pack_project(project_id, cancel_event=cancel_event, log_callback=log_callback)
        # T-147 freshness fusion: the plan walk IS the freshness walk. It sees
        # the SAME decisions as the packer (sampled-out media can never force
        # endless repacks), stats every file once for size AND mtime, and only
        # INCLUDED files newer than the archive invalidate it. A traversal or
        # stat failure (plan.failed > 0) means freshness cannot be proven, so
        # we repack rather than reuse an archive that may miss changed files.
        try:
            plan = build_plan_from_config(
                source,
                self.config.packing,
                excludes,
                newer_than_mtime=archive_mtime,
                # PERF-004: a reuse decision consumes no pruned-tree census, so
                # deciding freshness must not enumerate the excluded weight
                # (node_modules/.venv/.git) the census exists to describe.
                census_pruned=False,
            )
        except OSError:
            return self.pack_project(project_id, cancel_event=cancel_event, log_callback=log_callback)
        if plan.failed > 0 or plan.newer_found:
            return self.pack_project(project_id, cancel_event=cancel_event, log_callback=log_callback)
        # CORE-006: a reused archive reports the policy metadata from its OWN
        # validated manifest. Returning blank fields is what masked the mismatch
        # from every caller, including the audit path that consumes the archive.
        # Non-None here: archive_policy_mismatch above rejected a None manifest.
        manifest = manifest or {}
        return PackResult(
            project_id=project_id,
            name=proj.display_name,
            source_path=str(source),
            output_path=existing,
            success=True,
            fidelity_profile=str(manifest.get("fidelity_profile", "")),
            archive_semantics=str(manifest.get("archive_semantics", "")),
        )
