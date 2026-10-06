"""Canonical archive-freshness contract (PERF-002, audit/9.md).

One read-only probe answers ``FRESH`` / ``STALE`` / ``UNKNOWN`` for a project's
archive, and it is the SAME decision code the packer runs. Before this module
Project Room owned a second, drifted algorithm: a raw ``os.walk`` that applied
neither the configured/mandatory packing excludes nor the fidelity inclusion
decisions, so a newer ``node_modules/cache.js`` -- material that can never enter
the archive -- reported the archive stale, and >1,000 excluded files could eat
the bounded budget and prevent any verdict at all. Its result was also carried
in a boolean literally named ``source_older`` whose True/False the delegate read
in the opposite direction.

Framework-neutral on purpose: no Qt, no registry, no service. Callers that own a
registry (``PackingService``) resolve the project first; callers that already
hold a ``Project`` (the Project Room worker) call ``probe_archive_freshness``
directly. There is exactly one traversal policy, so producer and consumer cannot
drift again.

This module NEVER writes: it does not pack, delete, or replace an archive.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from audapack.config import app_dir
from audapack.fidelity import build_plan_from_config, policy_fingerprint_from_config
from audapack.packing import (
    archive_policy_mismatch,
    find_archive_for_project,
    read_archive_manifest,
    resolve_output_dir,
)


class ArchiveFreshness(str, Enum):
    """Explicit tri-state. There is no boolean spelling of this answer."""

    #: Current policy and every INCLUDED source file prove the archive is current.
    FRESH = "FRESH"
    #: Canonical policy proves a repack is required.
    STALE = "STALE"
    #: Freshness could not be proven. Never rendered or consumed as FRESH.
    UNKNOWN = "UNKNOWN"


@dataclass
class ArchiveFreshnessResult:
    """Structured verdict shared by the UI probe and the packing path."""

    state: ArchiveFreshness
    #: Short machine-ish reason, safe for logs and hover text.
    reason: str
    archive_path: Optional[Path] = None
    archive_mtime: Optional[float] = None
    #: True when the canonical packing policy requires a repack. STALE always
    #: implies this; an UNKNOWN caused by a failed traversal does too, because an
    #: archive that may be missing changed files must not be reused.
    repack_required: bool = False
    #: The probe stopped because its caller (or budget) cancelled it. A cancelled
    #: probe is a non-verdict, never an instruction to pack.
    cancelled: bool = False
    #: True when the archive's packing policy identity no longer matches the
    #: active configuration. ``reason`` then carries the packer's own message.
    policy_mismatch: bool = False
    manifest: Optional[dict] = None
    #: The freshness walk's plan, when it is complete enough to hand to the
    #: packer without re-walking the tree (``plan_reusable``).
    plan: Any = None
    plan_reusable: bool = False

    @property
    def is_fresh(self) -> bool:
        return self.state is ArchiveFreshness.FRESH

    @property
    def is_stale(self) -> bool:
        return self.state is ArchiveFreshness.STALE


class _BudgetCancel:
    """Deadline cancel token, optionally chained to a caller's own event.

    The UI probe must stay bounded, and a bounded probe that ran out of budget
    has NOT proven freshness -- it becomes ``UNKNOWN``, never ``FRESH``.
    """

    def __init__(self, budget_s: float, outer=None):
        self._deadline = time.monotonic() + max(0.0, float(budget_s))
        self._outer = outer

    def is_set(self) -> bool:
        if self._outer is not None:
            try:
                if self._outer.is_set():
                    return True
            except Exception:
                pass
        return time.monotonic() >= self._deadline


def probe_archive_freshness(
    project,
    packing_config,
    *,
    output_dir: Optional[Path] = None,
    cancel_event=None,
    budget_s: Optional[float] = None,
    fallback_dir: Optional[Path] = None,
) -> ArchiveFreshnessResult:
    """Read-only freshness verdict for ``project`` under ``packing_config``.

    Honours the configured excludes, the mandatory excludes, the fidelity
    inclusion decisions, the packing policy fingerprint and the existing archive
    manifest -- because it runs the packer's own plan builder rather than a
    second traversal. ``budget_s`` bounds the walk; exhausting it yields
    ``UNKNOWN``.
    """
    if project is None or not getattr(project, "source_path", ""):
        return ArchiveFreshnessResult(
            state=ArchiveFreshness.UNKNOWN,
            reason="project has no source path",
        )
    source = Path(project.source_path)
    try:
        if not source.exists():
            return ArchiveFreshnessResult(
                state=ArchiveFreshness.UNKNOWN,
                reason="source path is missing",
            )
    except OSError as exc:
        return ArchiveFreshnessResult(
            state=ArchiveFreshness.UNKNOWN,
            reason=f"source path unreadable: {exc}",
        )

    if output_dir is None:
        output_dir = resolve_output_dir(
            project.source_path,
            packing_config,
            fallback=fallback_dir or app_dir(),
            group=getattr(project, "priority_group", None),
            project=project,
        )
    existing = find_archive_for_project(project, output_dir)
    if not (existing and existing.is_file()):
        # Nothing to reuse means nothing to prove.
        return ArchiveFreshnessResult(
            state=ArchiveFreshness.STALE,
            reason="no archive",
            repack_required=True,
        )
    try:
        archive_mtime = float(existing.stat().st_mtime)
    except OSError as exc:
        # The archive cannot be read, so canonical policy already requires a
        # repack: that is a deterministic STALE, not an unproven verdict.
        return ArchiveFreshnessResult(
            state=ArchiveFreshness.STALE,
            reason=f"archive unreadable: {exc}",
            archive_path=existing,
            repack_required=True,
        )

    excludes = set(getattr(packing_config, "excludes", None) or [])
    fingerprint = policy_fingerprint_from_config(packing_config, excludes)
    manifest = read_archive_manifest(existing)
    mismatch = archive_policy_mismatch(existing, source, fingerprint, manifest)
    if mismatch is not None:
        return ArchiveFreshnessResult(
            state=ArchiveFreshness.STALE,
            reason=mismatch,
            archive_path=existing,
            archive_mtime=archive_mtime,
            repack_required=True,
            policy_mismatch=True,
            manifest=manifest,
        )

    # The source root's own mtime settles the common case before any walk.
    try:
        if source.stat().st_mtime > archive_mtime:
            return ArchiveFreshnessResult(
                state=ArchiveFreshness.STALE,
                reason="source root changed after pack",
                archive_path=existing,
                archive_mtime=archive_mtime,
                repack_required=True,
                manifest=manifest,
            )
    except OSError as exc:
        return ArchiveFreshnessResult(
            state=ArchiveFreshness.UNKNOWN,
            reason=f"source root stat failed: {exc}",
            archive_path=existing,
            archive_mtime=archive_mtime,
            repack_required=True,
            manifest=manifest,
        )

    probe_cancel = cancel_event
    if budget_s is not None:
        probe_cancel = _BudgetCancel(budget_s, outer=cancel_event)
    try:
        plan = build_plan_from_config(
            source,
            packing_config,
            excludes,
            newer_than_mtime=archive_mtime,
            # A freshness decision consumes no pruned-tree census, so it must not
            # enumerate the excluded weight the census exists to describe.
            census_pruned=False,
            cancel_event=probe_cancel,
        )
    except OSError as exc:
        return ArchiveFreshnessResult(
            state=ArchiveFreshness.UNKNOWN,
            reason=f"source traversal failed: {exc}",
            archive_path=existing,
            archive_mtime=archive_mtime,
            repack_required=True,
            manifest=manifest,
        )

    if plan.newer_found:
        # An INCLUDED file newer than the archive is sufficient evidence even
        # from a prefix of the tree.
        reusable = (
            not plan.stopped_on_stale
            and not plan.walk_incomplete
            and not plan.cancelled
            and plan.failed == 0
        )
        return ArchiveFreshnessResult(
            state=ArchiveFreshness.STALE,
            reason="included source file newer than archive",
            archive_path=existing,
            archive_mtime=archive_mtime,
            repack_required=True,
            manifest=manifest,
            plan=plan,
            plan_reusable=reusable,
        )
    if plan.cancelled:
        return ArchiveFreshnessResult(
            state=ArchiveFreshness.UNKNOWN,
            reason="freshness probe cancelled or out of budget",
            archive_path=existing,
            archive_mtime=archive_mtime,
            cancelled=True,
            manifest=manifest,
            plan=plan,
        )
    if plan.failed > 0 or plan.walk_incomplete:
        # Files that were never discovered cannot be proven older.
        return ArchiveFreshnessResult(
            state=ArchiveFreshness.UNKNOWN,
            reason="source traversal incomplete",
            archive_path=existing,
            archive_mtime=archive_mtime,
            repack_required=True,
            manifest=manifest,
            plan=plan,
        )
    return ArchiveFreshnessResult(
        state=ArchiveFreshness.FRESH,
        reason="archive current for the active packing policy",
        archive_path=existing,
        archive_mtime=archive_mtime,
        manifest=manifest,
        plan=plan,
    )
