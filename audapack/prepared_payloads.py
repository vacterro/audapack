"""Project-aware SAIHANDOFF resolution and durable pinned payloads."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from audapack.config import get_state_dir
from audapack.handoff_drop import detect_handoff, project_key


@dataclass(frozen=True)
class ResolvedHandoff:
    path: Path
    project_id: str
    sha256: str
    size: int
    captured_at_epoch: float
    source: str


def _aliases(project: object) -> set[str]:
    values = (
        getattr(project, "id", ""), getattr(project, "display_name", ""),
        getattr(project, "audit_project_name", ""),
        *(getattr(project, "inaudit_aliases", None) or ()),
    )
    return {project_key(value) for value in values if len(project_key(value)) >= 3}


def _owner(label: str, projects: Iterable[object]) -> str | None:
    key = project_key(label)
    matches = [str(project.id) for project in projects if key in _aliases(project)]
    return matches[0] if len(matches) == 1 else None


def _read_verified(path: Path, max_bytes: int = 2_000_000) -> tuple[bytes, str] | None:
    try:
        if not path.is_file() or path.stat().st_size > max_bytes:
            return None
        body = path.read_bytes()
        text = body.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    return body, text


def resolve_latest_handoff(project_id: str, projects: Iterable[object], folder: Path) -> ResolvedHandoff | None:
    """Choose newest content-verified handoff belonging to exactly one project."""
    projects = tuple(projects)
    if not any(str(getattr(project, "id", "")) == project_id for project in projects):
        raise ValueError("project is not registered")
    if not folder.is_dir():
        return None
    known = set().union(*(_aliases(project) for project in projects))
    candidates: list[ResolvedHandoff] = []
    for path in folder.glob("*.md"):
        read = _read_verified(path)
        if read is None:
            continue
        body, text = read
        label = detect_handoff(text, known)
        if not label or _owner(label, projects) != project_id:
            continue
        digest = hashlib.sha256(body).hexdigest()
        candidates.append(ResolvedHandoff(
            path=path, project_id=project_id, sha256=digest, size=len(body),
            captured_at_epoch=path.stat().st_mtime, source="handoff_drop",
        ))
    if not candidates:
        return None
    # Duplicate content is one identity. Deterministic ties use hash and path.
    by_hash: dict[str, ResolvedHandoff] = {}
    for candidate in candidates:
        previous = by_hash.get(candidate.sha256)
        if previous is None or (candidate.captured_at_epoch, str(candidate.path)) > (previous.captured_at_epoch, str(previous.path)):
            by_hash[candidate.sha256] = candidate
    return max(by_hash.values(), key=lambda item: (item.captured_at_epoch, item.sha256))


def inspect_handoff(source: Path, expected_project_id: str,
                    projects: Iterable[object]) -> tuple[bytes, ResolvedHandoff]:
    """Validate a handoff for dry run without writing a durable copy."""
    read = _read_verified(source)
    if read is None:
        raise ValueError("handoff missing, too large, or not UTF-8")
    body, text = read
    projects = tuple(projects)
    known = set().union(*(_aliases(project) for project in projects))
    label = detect_handoff(text, known)
    if not label or _owner(label, projects) != expected_project_id:
        raise ValueError("handoff project identity mismatch or ambiguous")
    digest = hashlib.sha256(body).hexdigest()
    return body, ResolvedHandoff(source, expected_project_id, digest, len(body),
                                 source.stat().st_mtime, "selected_original")


def pin_handoff(source: Path, expected_project_id: str, projects: Iterable[object],
                storage: Path | None = None) -> ResolvedHandoff:
    """Copy one validated handoff into immutable local runtime storage."""
    body, inspected = inspect_handoff(source, expected_project_id, projects)
    digest = inspected.sha256
    folder = Path(storage) if storage is not None else get_state_dir() / "prepared_handoffs"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{digest}.md"
    try:
        with target.open("xb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        pass
    if target.read_bytes() != body:
        raise ValueError("pinned handoff hash mismatch")
    return ResolvedHandoff(target, expected_project_id, digest, len(body),
                           source.stat().st_mtime, "pinned_copy")


def read_pinned(path: Path, expected_sha256: str) -> bytes:
    read = _read_verified(path)
    if read is None:
        raise ValueError("pinned handoff missing")
    body, _ = read
    if hashlib.sha256(body).hexdigest() != expected_sha256:
        raise ValueError("pinned handoff content changed")
    return body
