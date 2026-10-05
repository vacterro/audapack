"""Packing operations — thin wrapper over the existing engine."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, Optional

from audapack import hot_freshness
from audapack.config import AppConfig, app_dir, load_config
from audapack.fidelity import policy_fingerprint_from_config
from audapack.freshness import probe_archive_freshness
from audapack.models import PackResult
from audapack.packing import find_archive_for_project, pack_single, resolve_output_dir
from audapack.projects import ProjectRegistry
from audapack.saipen import get_saipen_info


class PackingService:
    def __init__(self, config: Optional[AppConfig] = None, base_dir=None):
        self.base_dir = base_dir
        self.config = config or load_config(base_dir)
        self.registry = ProjectRegistry(self.config, base_dir=base_dir, transactional=True)

    def _policy_fingerprint(self) -> str:
        """The ONE fingerprint a hot proof is keyed by, for pack and ensure alike."""
        return policy_fingerprint_from_config(
            self.config.packing, set(self.config.packing.excludes or [])
        )

    def pack_project(
        self,
        project_id: str,
        *,
        progress_callback: Optional[Callable] = None,
        cancel_event=None,
        log_callback: Optional[Callable[[str], None]] = None,
        prebuilt_plan=None,
        observed_by_caller: bool = False,
    ):
        """Pack now, and seed the next ensure's hot proof when that is sound.

        T-237: a direct pack (UI PACK, autopack before audit) used to leave no
        proof, so the very next unchanged ensure repeated the full source walk
        the pack had just done. The watch is armed BEFORE the pack walks the
        source and the proof is confirmed only for a successful archive with the
        same policy fingerprint, exactly like the repack leg of
        `ensure_fresh_archive` -- so a mutation during the pack, a failed or
        cancelled pack, or an unavailable monitor records nothing.

        ``observed_by_caller`` is set only by `ensure_fresh_archive`, which armed
        the watch before its own probe and records the proof itself; this call
        then neither re-arms nor confirms, so one transaction has one owner.
        """
        pack = lambda: self._pack(  # noqa: E731 - one call, two owners
            project_id,
            progress_callback=progress_callback,
            cancel_event=cancel_event,
            log_callback=log_callback,
            prebuilt_plan=prebuilt_plan,
        )
        if observed_by_caller:
            return pack()
        proj = self.registry.get_project_by_id(project_id)
        source_path = getattr(proj, "source_path", "") if proj else ""
        fingerprint = self._policy_fingerprint() if source_path else ""
        arm_token = hot_freshness.begin(source_path, fingerprint) if source_path else None
        result = pack()
        cancelled = cancel_event is not None and cancel_event.is_set()
        if result is not None and result.success and result.output_path and not cancelled:
            self._record_hot_proof(
                arm_token, fingerprint, result.output_path,
                fidelity_profile=result.fidelity_profile,
                archive_semantics=result.archive_semantics,
            )
        return result

    def _pack(
        self,
        project_id: str,
        *,
        progress_callback: Optional[Callable] = None,
        cancel_event=None,
        log_callback: Optional[Callable[[str], None]] = None,
        prebuilt_plan=None,
    ):
        """The pack itself. Hot-proof ownership stays with the caller."""
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
        if prebuilt_plan is not None:
            # T-155 (PERF-004): the freshness probe's complete stale plan is the
            # pack plan -- same source, same config, same excludes, one tree
            # walk for both. The census is completed in pack_single before the
            # archive is written.
            from audapack.fidelity import finalize_pruned_census

            finalize_pruned_census(prebuilt_plan)
            prebuilt_plan.pruned_census_taken = True
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
            prebuilt_plan=prebuilt_plan,
        )

    def ensure_fresh_archive(self, project_id: str, *, cancel_event=None, log_callback=None):
        """Return a current archive, packing only when the source is newer.

        The returned path is either an existing archive proven to belong to the
        registered project/output directory or the output of a successful
        ``PackResult``. Failed/partial packs never become trusted artifacts.

        P1 TARGET F: this is the ONE owner of the hot freshness fast path. A hot
        proof is consulted first and, when it holds, the full source walk is
        skipped; a monitor is armed BEFORE the authoritative probe so a proof
        can only be recorded for a probe that ran under continuous observation.
        """
        proj = self.registry.get_project_by_id(project_id)
        if not proj or not proj.enabled or not proj.source_path:
            return PackResult(project_id=project_id, name=getattr(proj, "display_name", project_id), source_path="", success=False, error_message="Project is missing, disabled, or has no source path")
        source = Path(proj.source_path)
        if not source.exists():
            return PackResult(project_id=project_id, name=proj.display_name, source_path=str(source), success=False, error_message="Project source path is missing")

        output_dir = resolve_output_dir(
            proj.source_path,
            self.config.packing,
            fallback=app_dir(),
            group=proj.priority_group,
            project=proj,
        )
        fingerprint = self._policy_fingerprint()
        existing = find_archive_for_project(proj, output_dir)
        hot = hot_freshness.lookup(proj.source_path, fingerprint, existing)
        if hot is not None:
            # O(1): the monitor saw no mutation since a verified pack/probe, the
            # policy is unchanged and the archive is byte-identical by path +
            # size + mtime_ns + ctime_ns. Nothing here is inferred from a clock.
            return PackResult(
                project_id=project_id,
                name=proj.display_name,
                source_path=str(source),
                # `existing` is the path `lookup` just verified; the proof's own
                # key is case-folded on Windows and must not become the name.
                output_path=Path(existing),
                success=True,
                fidelity_profile=hot.fidelity_profile,
                archive_semantics=hot.archive_semantics,
                reused=True,
                timings={
                    "freshness_probe_ms": 0.0,
                    "hot_proof": True,
                    "source_walk_skipped": True,
                },
            )
        # Armed BEFORE the probe. Anything the probe does not witness is not a
        # proof, so a mutation during the walk cannot become a clean generation.
        arm_token = hot_freshness.begin(proj.source_path, fingerprint)

        # PERF-002 (audit/9.md): the freshness decision itself lives in ONE
        # framework-neutral probe (``audapack.freshness``) that Project Room also
        # consumes read-only. Everything the reuse gate used to inline here --
        # the policy fingerprint, the manifest read, the source-root shortcut and
        # the fidelity plan walk that IS the freshness walk -- now happens there,
        # so the UI verdict and the packer verdict cannot drift apart again.
        probe_started = time.perf_counter()
        verdict = probe_archive_freshness(
            proj,
            self.config.packing,
            output_dir=output_dir,
            cancel_event=cancel_event,
            fallback_dir=app_dir(),
        )
        probe_ms = (time.perf_counter() - probe_started) * 1000.0
        if verdict.cancelled:
            # T-155: the operator cancelled the probe. Return promptly; never
            # start a pack on behalf of a cancelled decision.
            return PackResult(
                project_id=project_id,
                name=proj.display_name,
                source_path=str(source),
                success=False,
                error_message="Cancelled by user",
            )
        if verdict.policy_mismatch and log_callback:
            log_callback(f"repacking {proj.display_name}: {verdict.reason}")
        if verdict.repack_required:
            # A plan the probe stopped early (stopped_on_stale) is only a prefix
            # of the tree, and a failed walk may have missed files: both are
            # verdicts, not pack inputs, so the pack plans for itself. A complete
            # stale plan IS the pack plan -- the census is completed in
            # pack_single, costing no source traversal.
            pack_started = time.perf_counter()
            # This transaction already holds the arm taken before its probe,
            # so the pack must neither re-arm nor confirm (T-237).
            result = self.pack_project(
                project_id,
                cancel_event=cancel_event,
                log_callback=log_callback,
                prebuilt_plan=verdict.plan if verdict.plan_reusable else None,
                observed_by_caller=True,
            )
            pack_ms = (time.perf_counter() - pack_started) * 1000.0
            # T-180: the archive was (re)packed this call, not reused.
            if result is not None:
                result.packed = True
                result.timings = {
                    "freshness_probe_ms": probe_ms,
                    "archive_pack_ms": pack_ms,
                    "hot_proof": False,
                    "source_walk_skipped": False,
                }
                if result.success and result.output_path:
                    # TARGET L: the NEXT unchanged click must be hot. Recording
                    # the proof here is what makes that true, and it is recorded
                    # only for a pack this monitor watched from start to finish.
                    self._record_hot_proof(
                        arm_token, fingerprint, result.output_path,
                        fidelity_profile=result.fidelity_profile,
                        archive_semantics=result.archive_semantics,
                    )
            return result
        existing = verdict.archive_path
        # CORE-006: a reused archive reports the policy metadata from its OWN
        # validated manifest. Returning blank fields is what masked the mismatch
        # from every caller, including the audit path that consumes the archive.
        # Non-None here: the policy gate above rejected a None manifest.
        manifest = verdict.manifest or {}
        # Only a FULL, uncancelled FRESH verdict may become a hot proof; an
        # UNKNOWN never reaches this line because it sets repack_required.
        if verdict.is_fresh:
            self._record_hot_proof(
                arm_token, fingerprint, existing,
                fidelity_profile=str(manifest.get("fidelity_profile", "")),
                archive_semantics=str(manifest.get("archive_semantics", "")),
            )
        return PackResult(
            project_id=project_id,
            name=proj.display_name,
            source_path=str(source),
            output_path=existing,
            success=True,
            fidelity_profile=str(manifest.get("fidelity_profile", "")),
            archive_semantics=str(manifest.get("archive_semantics", "")),
            reused=True,
            timings={
                "freshness_probe_ms": probe_ms,
                "hot_proof": False,
                "source_walk_skipped": False,
            },
        )

    @staticmethod
    def _record_hot_proof(
        arm_token,
        fingerprint: str,
        archive,
        *,
        fidelity_profile: str = "",
        archive_semantics: str = "",
    ) -> bool:
        """Best-effort hot proof. Failure only costs the next click a full walk."""
        try:
            return hot_freshness.confirm(
                arm_token,
                archive,
                fingerprint,
                fidelity_profile=fidelity_profile,
                archive_semantics=archive_semantics,
            )
        except Exception:  # noqa: BLE001 - acceleration must never break ensure
            return False
