"""Packing operations — thin wrapper over the existing engine."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Iterable, List, Optional

from audapack.config import AppConfig, app_dir, load_config
from audapack.packing import pack_single, resolve_output_dir
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
        )

    def pack_path(self, path: str | Path, **kw):
        target = Path(path).resolve()
        output_dir = resolve_output_dir(target, self.config.packing, fallback=app_dir())
        excludes = set(self.config.packing.excludes)
        stem = target.name
        inc_ts = kw.pop("include_timestamp", getattr(self.config.packing, "include_timestamp", True))
        return pack_single(source_path=target, output_dir=output_dir, archive_stem=stem, excludes=excludes, include_timestamp=inc_ts, **kw)

    def pack_selected(self, project_ids: Iterable[str], **kw) -> List:
        results = []
        for pid in project_ids:
            results.append(self.pack_project(pid, **kw))
        return results
