"""Compact first-class metrics strip for the main AUDAPACK UI.

Visual parity with the status/metrics language across SAITULS, FastPrompter,
ZAICODE and related SAI tools:
- immediately readable;
- compact;
- always visible;
- based on real project/archive state;
- no fabricated percentages or decorative fluff.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QHBoxLayout,
    QSizePolicy,
    QWidget,
)

from audapack.config import app_dir
from audapack.fidelity import policy_fingerprint_from_config
from audapack.freshness import ArchiveFreshness
from audapack.models import Project
from audapack.packing import (
    archive_policy_mismatch,
    find_archive_for_project,
    human_mb,
    read_archive_manifest,
    resolve_output_dir,
)
from audapack.ui_qt.theme.golden_default import GoldenDefault


@dataclass
class MetricItem:
    """One discrete metric in the strip."""

    label: str
    value: str
    percent: Optional[int] = None  # 0..100 for bar/fill, or None
    state: str = "neutral"  # healthy, warning, danger, neutral, active
    tooltip: str = ""


@dataclass
class ProjectMetricsSnapshot:
    """Canonical UI-facing metrics snapshot for a project."""

    project_id: str
    source: MetricItem
    evidence: MetricItem
    freshness: MetricItem
    size: MetricItem
    readiness: MetricItem
    activity: Optional[MetricItem] = None


# Semantic color palettes matching Golden Default (Win95 Dark Golden)
METRIC_COLORS = {
    "healthy": {
        "text": "#55FF55",
        "fill": QColor(74, 122, 32, 110),
        "border": QColor(74, 122, 32),
    },
    "warning": {
        "text": "#F0D060",
        "fill": QColor(122, 122, 32, 110),
        "border": QColor(122, 122, 32),
    },
    "danger": {
        "text": "#FF6B6B",
        "fill": QColor(122, 32, 32, 120),
        "border": QColor(122, 32, 32),
    },
    "active": {
        "text": "#66D9EF",
        "fill": QColor(0, 100, 100, 120),
        "border": QColor(0, 128, 128),
    },
    "neutral": {
        "text": "#9C9371",
        "fill": QColor(50, 45, 35, 80),
        "border": QColor(60, 54, 42),
    },
}


def evaluate_project_metrics(
    project: Optional[Project],
    config: Any,
    output_dir: Optional[Path] = None,
    current_pack_state: tuple[str, str] = ("IDLE", ""),
    is_packing: bool = False,
    is_batch_busy: bool = False,
    pack_progress: Optional[dict] = None,
    canonical_freshness: Optional[Any] = None,
) -> ProjectMetricsSnapshot:
    """Deterministically derive the 5 primary metrics + activity from real facts."""
    pid = project.id if project else "none"

    # Default fallback metrics when project is absent or unconfigured
    if not project or not project.source_path or not project.source_path.strip():
        return ProjectMetricsSnapshot(
            project_id=pid,
            source=MetricItem("SOURCE", "N/A", None, "neutral", "Packed source completeness for audit."),
            evidence=MetricItem("EVID", "N/A", None, "neutral", "Required audit evidence included."),
            freshness=MetricItem("FRESH", "N/A", None, "neutral", "Whether this archive still matches the project."),
            size=MetricItem("SIZE", "N/A", None, "neutral", "Archive size versus the target budget."),
            readiness=MetricItem("READY", "NONE", None, "neutral", "Overall audit readiness."),
            activity=MetricItem("PACK", "IDLE", None, "neutral", "No packing active."),
        )

    src_path = Path(project.source_path)
    out_dir = output_dir or resolve_output_dir(
        src_path, config.packing, fallback=app_dir(), group=project.priority_group, project=project
    )
    archive = find_archive_for_project(project, out_dir) if out_dir.exists() else None
    manifest = read_archive_manifest(archive) if (archive and archive.exists()) else None

    # 1. SOURCE
    if archive is None or not archive.exists():
        source_metric = MetricItem(
            "SOURCE", "NONE", None, "neutral", "Packed source completeness for audit."
        )
    elif manifest is None:
        source_metric = MetricItem(
            "SOURCE", "?", None, "warning", "Packed source completeness for audit. Manifest unreadable."
        )
    else:
        sr_ready = manifest.get("source_review_ready")
        sr = manifest.get("source_review") or {}
        components = sr.get("components") or []
        if components:
            inc = sum(1 for c in components if c.get("included"))
            tot = len(components)
            pct = int(inc * 100 / tot) if tot > 0 else 100
            if pct == 100 and sr_ready is not False:
                source_metric = MetricItem(
                    "SOURCE", "100%", 100, "healthy", "Packed source completeness for audit."
                )
            elif pct > 0:
                source_metric = MetricItem(
                    "SOURCE", f"{pct}%", pct, "warning", "Packed source completeness for audit. Nested components omitted."
                )
            else:
                source_metric = MetricItem(
                    "SOURCE", "BLOCKED", 0, "danger", "Packed source completeness for audit. Required source missing."
                )
        else:
            if sr_ready is True:
                source_metric = MetricItem(
                    "SOURCE", "100%", 100, "healthy", "Packed source completeness for audit."
                )
            elif sr_ready is False:
                source_metric = MetricItem(
                    "SOURCE", "BLOCKED", 0, "danger", "Packed source completeness for audit. Source review failed."
                )
            else:
                source_metric = MetricItem(
                    "SOURCE", "?", None, "warning", "Packed source completeness for audit. Legacy archive without source review."
                )

    # 2. EVIDENCE
    if archive is None or not archive.exists() or manifest is None:
        evid_metric = MetricItem(
            "EVID", "N/A", None, "neutral", "Required audit evidence included."
        )
    else:
        has_saipen_dir = bool(src_path and (src_path / ".saipen").is_dir())
        saipen = manifest.get("saipen_snapshot") or manifest.get("saipen_evidence")
        saipen_detected = bool(
            manifest.get("saipen_detected") or (saipen and saipen.get("detected")) or has_saipen_dir or bool(saipen)
        )
        if not saipen_detected:
            evid_metric = MetricItem(
                "EVID", "N/A", None, "neutral", "Required audit evidence included. Not a SAIPEN project."
            )
        elif not isinstance(saipen, dict):
            evid_metric = MetricItem(
                "EVID", "?", None, "warning", "Required audit evidence included. Snapshot missing."
            )
        else:
            req_omitted = bool(saipen.get("required_evidence_omitted", False))
            missing = list(saipen.get("mandatory_missing") or saipen.get("omitted_required") or [])
            declared = list(saipen.get("mandatory_declared") or saipen.get("required_declared") or [])
            if req_omitted or missing:
                if declared:
                    incl = max(0, len(declared) - len(missing))
                    pct = max(0, min(100, int(incl * 100 / len(declared))))
                    evid_metric = MetricItem(
                        "EVID", f"{pct}%", pct, "warning", "Required audit evidence included."
                    )
                else:
                    evid_metric = MetricItem(
                        "EVID", "PARTIAL", 50, "warning", "Required audit evidence included."
                    )
            else:
                evid_metric = MetricItem(
                    "EVID", "100%", 100, "healthy", "Required audit evidence included."
                )

    # 3. FRESHNESS
    if archive is None or not archive.exists():
        fresh_metric = MetricItem(
            "FRESH", "REPACK", None, "warning", "Whether this archive still matches the project."
        )
    else:
        expected_fp = policy_fingerprint_from_config(
            config.packing, set(config.packing.excludes or []), project=project
        )
        mismatch = archive_policy_mismatch(archive, src_path, expected_fp, manifest)
        if mismatch:
            if "predates" in mismatch or "no producer block" in mismatch or "schema" in mismatch:
                fresh_metric = MetricItem(
                    "FRESH", "LEGACY", None, "warning", "Whether this archive still matches the project. Legacy archive."
                )
            else:
                fresh_metric = MetricItem(
                    "FRESH", "STALE", None, "warning", "Whether this archive still matches the project. Archive is stale."
                )
        else:
            git_dirty = manifest.get("git_dirty") if manifest else None
            if git_dirty is True:
                fresh_metric = MetricItem(
                    "FRESH", "DIRTY", 100, "warning", "Whether this archive still matches the project. Uncommitted changes present."
                )
            else:
                cf = canonical_freshness
                if cf is None:
                    try:
                        from audapack import hot_freshness
                        proof = hot_freshness.lookup(src_path, expected_fp, archive)
                        if proof is not None:
                            cf = ArchiveFreshness.FRESH
                    except Exception:
                        pass

                cf_str = (cf.value if isinstance(cf, ArchiveFreshness) else str(cf or "")).upper()
                if cf_str == "FRESH":
                    fresh_metric = MetricItem(
                        "FRESH", "FRESH", 100, "healthy", "Whether this archive still matches the project."
                    )
                elif cf_str == "STALE":
                    fresh_metric = MetricItem(
                        "FRESH", "STALE", None, "warning", "Whether this archive still matches the project. Source modified since pack."
                    )
                elif cf_str in ("UNKNOWN", "CHECKING", "PENDING", ""):
                    fresh_metric = MetricItem(
                        "FRESH", "UNKNOWN", None, "warning", "Whether this archive still matches the project. Freshness unverified."
                    )
                else:
                    fresh_metric = MetricItem(
                        "FRESH", cf_str, None, "warning", f"Whether this archive still matches the project. {cf_str}"
                    )

    # 4. SIZE / BUDGET
    if archive is None or not archive.exists():
        size_metric = MetricItem(
            "SIZE", "N/A", None, "neutral", "Archive size versus the target budget."
        )
    else:
        try:
            size_bytes = archive.stat().st_size
            size_str = human_mb(size_bytes)
            target_mb = int(getattr(config.packing, "fidelity_max_mb", 30) or 30)
            budget_bytes = int(
                manifest.get("budget_bytes") or (target_mb * 1024 * 1024)
            ) if manifest else (target_mb * 1024 * 1024)
            pct = int(size_bytes * 100 / budget_bytes) if budget_bytes > 0 else 0
            if size_bytes <= budget_bytes:
                size_metric = MetricItem(
                    "SIZE", size_str, min(100, pct), "healthy", f"Archive size versus target budget ({human_mb(budget_bytes)})."
                )
            else:
                size_metric = MetricItem(
                    "SIZE", size_str, min(100, pct), "warning", f"Archive size ({size_str}) exceeds target budget ({human_mb(budget_bytes)})."
                )
        except OSError:
            size_metric = MetricItem(
                "SIZE", "ERR", None, "danger", "Archive size versus the target budget."
            )

    # 5. READINESS
    pack_st = current_pack_state[0] if current_pack_state else "IDLE"
    if is_packing or pack_st in ("PACKING", "QUEUED"):
        ready_metric = MetricItem(
            "READY", "PACKING", None, "active", "Overall audit readiness. Packing in progress."
        )
    elif pack_st == "FAILED":
        ready_metric = MetricItem(
            "READY", "ERROR", None, "danger", "Overall audit readiness. Last pack failed."
        )
    elif archive is None or not archive.exists():
        ready_metric = MetricItem(
            "READY", "NONE", None, "neutral", "Overall audit readiness. Project not yet packed."
        )
    elif source_metric.state == "danger":
        ready_metric = MetricItem(
            "READY", "BLOCKED", None, "danger", "Overall audit readiness. Source incomplete for review."
        )
    elif fresh_metric.state == "warning" or evid_metric.state == "warning" or source_metric.state == "warning":
        ready_metric = MetricItem(
            "READY", "PARTIAL", None, "warning", "Overall audit readiness. Archive is stale or evidence/source partial."
        )
    else:
        ready_metric = MetricItem(
            "READY", "READY", 100, "healthy", "Overall audit readiness."
        )

    # 6. ACTIVITY
    if is_batch_busy:
        activity_metric = MetricItem(
            "PACK", "BATCH", None, "active", "Pack all batch active."
        )
    elif is_packing or pack_st in ("PACKING", "QUEUED"):
        activity_metric = MetricItem(
            "PACK", "PACKING", None, "active", "Project packing active."
        )
    else:
        activity_metric = MetricItem(
            "PACK", "IDLE", None, "neutral", "No packing active."
        )

    return ProjectMetricsSnapshot(
        project_id=pid,
        source=source_metric,
        evidence=evid_metric,
        freshness=fresh_metric,
        size=size_metric,
        readiness=ready_metric,
        activity=activity_metric,
    )


class MetricCellWidget(QWidget):
    """One compact, beveled status meter cell in the metrics strip."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.item = MetricItem(label="", value="")
        self._show_tooltips: bool = True
        self._tooltip_duration_ms: int = 10000
        self.setFixedHeight(22)
        self.setMinimumWidth(80)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.font_label = QFont("Verdana", 8)
        self.font_label.setBold(True)
        self.font_label.setStyleStrategy(QFont.StyleStrategy.NoAntialias)
        self.font_value = QFont("Verdana", 9)
        self.font_value.setBold(True)
        self.font_value.setStyleStrategy(QFont.StyleStrategy.NoAntialias)

    def set_tooltip_policy(self, show_tooltips: bool, duration_ms: int = -1) -> None:
        self._show_tooltips = bool(show_tooltips)
        self._tooltip_duration_ms = int(duration_ms)
        self._apply_tooltip()

    def _apply_tooltip(self) -> None:
        if not self._show_tooltips or not self.item.tooltip:
            self.setToolTip("")
            return
        tip = " ".join(self.item.tooltip.replace("\n", " ").split())
        if len(tip) > 80:
            tip = tip[:77] + "..."
        self.setToolTip(tip)
        if hasattr(self, "setToolTipDuration"):
            self.setToolTipDuration(max(-1, self._tooltip_duration_ms))

    def set_metric(
        self,
        item: MetricItem,
        *,
        show_tooltips: Optional[bool] = None,
        tooltip_duration_ms: Optional[int] = None,
    ) -> None:
        self.item = item
        if show_tooltips is not None:
            self._show_tooltips = bool(show_tooltips)
        if tooltip_duration_ms is not None:
            self._tooltip_duration_ms = int(tooltip_duration_ms)
        self._apply_tooltip()
        self.update()

    def sizeHint(self) -> QSize:
        return QSize(105, 22)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        rect = self.rect()

        # Inset / sunken background
        painter.fillRect(rect, QColor(GoldenDefault.backgroundSoft))

        # Fill bar if percent is defined
        colors = METRIC_COLORS.get(self.item.state, METRIC_COLORS["neutral"])
        pct = self.item.percent
        if pct is not None and pct > 0:
            fill_w = max(2, int((rect.width() - 4) * min(pct, 100) / 100.0))
            fill_rect = QRect(rect.left() + 2, rect.top() + 2, fill_w, rect.height() - 4)
            painter.fillRect(fill_rect, colors["fill"])

        # Sunken 3D border (Win95 style)
        pen_dark = QPen(QColor(GoldenDefault.borderDark))
        pen_light = QPen(QColor(GoldenDefault.bevelLight))
        painter.setPen(pen_dark)
        painter.drawLine(rect.left(), rect.top(), rect.right() - 1, rect.top())
        painter.drawLine(rect.left(), rect.top(), rect.left(), rect.bottom() - 1)
        painter.setPen(pen_light)
        painter.drawLine(rect.right(), rect.top(), rect.right(), rect.bottom())
        painter.drawLine(rect.left(), rect.bottom(), rect.right(), rect.bottom())

        # Render Label
        if self.item.label:
            painter.setFont(self.font_label)
            painter.setPen(QColor(GoldenDefault.textMuted))
            label_rect = QRect(rect.left() + 6, rect.top() + 1, 46, rect.height() - 2)
            painter.drawText(
                label_rect,
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                self.item.label,
            )

        # Render Value
        if self.item.value:
            painter.setFont(self.font_value)
            painter.setPen(QColor(colors["text"]))
            val_rect = QRect(rect.left() + 52, rect.top() + 1, rect.width() - 56, rect.height() - 2)
            painter.drawText(
                val_rect,
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                self.item.value,
            )

        painter.end()


class MetricsStripWidget(QWidget):
    """The dedicated horizontal metrics strip."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._show_tooltips: bool = True
        self._tooltip_duration_ms: int = 10000
        self.setFixedHeight(26)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        self.layout = QHBoxLayout(self)
        self.layout.setContentsMargins(4, 2, 4, 2)
        self.layout.setSpacing(3)

        self.cell_source = MetricCellWidget(self)
        self.cell_evidence = MetricCellWidget(self)
        self.cell_fresh = MetricCellWidget(self)
        self.cell_size = MetricCellWidget(self)
        self.cell_ready = MetricCellWidget(self)
        self.cell_activity = MetricCellWidget(self)

        self.layout.addWidget(self.cell_source)
        self.layout.addWidget(self.cell_evidence)
        self.layout.addWidget(self.cell_fresh)
        self.layout.addWidget(self.cell_size)
        self.layout.addWidget(self.cell_ready)
        self.layout.addWidget(self.cell_activity)

    def set_tooltip_policy(self, show_tooltips: bool, duration_ms: int = -1) -> None:
        self._show_tooltips = bool(show_tooltips)
        self._tooltip_duration_ms = int(duration_ms)
        for cell in (
            self.cell_source,
            self.cell_evidence,
            self.cell_fresh,
            self.cell_size,
            self.cell_ready,
            self.cell_activity,
        ):
            cell.set_tooltip_policy(self._show_tooltips, self._tooltip_duration_ms)

    def update_snapshot(self, snapshot: ProjectMetricsSnapshot) -> None:
        self.cell_source.set_metric(
            snapshot.source,
            show_tooltips=self._show_tooltips,
            tooltip_duration_ms=self._tooltip_duration_ms,
        )
        self.cell_evidence.set_metric(
            snapshot.evidence,
            show_tooltips=self._show_tooltips,
            tooltip_duration_ms=self._tooltip_duration_ms,
        )
        self.cell_fresh.set_metric(
            snapshot.freshness,
            show_tooltips=self._show_tooltips,
            tooltip_duration_ms=self._tooltip_duration_ms,
        )
        self.cell_size.set_metric(
            snapshot.size,
            show_tooltips=self._show_tooltips,
            tooltip_duration_ms=self._tooltip_duration_ms,
        )
        self.cell_ready.set_metric(
            snapshot.readiness,
            show_tooltips=self._show_tooltips,
            tooltip_duration_ms=self._tooltip_duration_ms,
        )
        if snapshot.activity:
            self.cell_activity.set_metric(
                snapshot.activity,
                show_tooltips=self._show_tooltips,
                tooltip_duration_ms=self._tooltip_duration_ms,
            )
            self.cell_activity.setVisible(True)
        else:
            self.cell_activity.setVisible(False)

    def set_compact(self, compact: bool) -> None:
        if compact:
            self.layout.setContentsMargins(2, 1, 2, 1)
            self.layout.setSpacing(2)
            self.setFixedHeight(24)
        else:
            self.layout.setContentsMargins(4, 2, 4, 2)
            self.layout.setSpacing(3)
            self.setFixedHeight(26)
