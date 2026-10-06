"""Ready-to-hand-over SAIHANDOFF files.

An assistant reply that carries a SAIHANDOFF block is the next agent's whole
brief. Handing it over used to be manual: copy the block, paste it into a
scratchpad, export it as a file, then paste that file's path into the agent.
This module owns the last two steps. A captured block that is a SAIHANDOFF is
written, byte-exact, as ``<Project>_<YYYYMMDD>_<HHMM>.md`` (the same naming the
operator's scratchpad export already uses) into one drop folder, and its path
is what the operator hands to an agent.

Identity is content: the same block captured twice (a retry, a re-render, a
second click) resolves to the file already written for it, never to a second
copy. The folder index is an accelerator only -- a missing or edited file is
simply written again.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import re
import tempfile
from pathlib import Path
from typing import Any, Iterable, Optional

from audapack.bridge.storage import atomic_write

DROP_DIRNAME = "audapack_handoffs"
INDEX_NAME = ".audapack_handoffs.json"
INDEX_MAX_ENTRIES = 500
#: A SAIHANDOFF marker must appear this early; a reply that merely mentions the
#: word deep in its prose is not a handoff.
MARKER_WINDOW_LINES = 12
MAX_LABEL_LENGTH = 48
#: A project-addressed brief needs a real body under its name line.
MIN_BRIEF_LINES = 5

_MARKER_RE = re.compile(r"^SAIHANDOFF(?:\b|_)")
_TITLE_RE = re.compile(r"^(?P<project>[^\r\n]+?)\s+[—-]\s+SAIHANDOFF(?:\s+[—-]\s+.*)?$")
_ILLEGAL = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def default_drop_dir() -> Path:
    return Path(tempfile.gettempdir()) / DROP_DIRNAME


def resolve_drop_dir(configured: str = "") -> Path:
    value = str(configured or "").strip()
    return Path(value) if value else default_drop_dir()


def _clean_label(value: str) -> str:
    label = _ILLEGAL.sub("_", str(value or ""))
    label = " ".join(label.split()).strip(" ._")
    return label[:MAX_LABEL_LENGTH]


def project_key(value: str) -> str:
    """Case- and decoration-insensitive project identity: `__SAIMAIL__` == `SAIMAIL`."""
    return re.sub(r"[\s_\-.]+", "", str(value or "")).casefold()


def project_names(projects: Iterable[Any]) -> set[str]:
    """Every identity a registered project answers to, as `project_key` values."""
    keys: set[str] = set()
    for project in projects or ():
        for value in (
            getattr(project, "id", ""),
            getattr(project, "display_name", ""),
            getattr(project, "audit_project_name", ""),
            *(getattr(project, "inaudit_aliases", None) or ()),
        ):
            key = project_key(value)
            if len(key) >= 3:
                keys.add(key)
    return keys


def detect_handoff(text: str, known_projects: Iterable[str] = ()) -> Optional[str]:
    """The project label of a handoff block, or None when it is not one.

    Three accepted shapes, all taken from real handoffs:

    - ``<Project>`` on its own first line, then ``SAIHANDOFF ...`` within the
      first few non-empty lines;
    - the canonical one-line title ``<Project> — SAIHANDOFF — <topic>``;
    - a brief addressed to a REGISTERED project: its first line is exactly that
      project's name and a real body follows (``LIMISAW`` / ``MISSION`` / ...).
      ``known_projects`` holds `project_key` values; without them this shape is
      never guessed.
    """
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()][:MARKER_WINDOW_LINES]
    if not lines:
        return None
    title = _TITLE_RE.match(lines[0])
    if title:
        return _clean_label(title.group("project")) or "SAIHANDOFF"
    marker_at = next((index for index, line in enumerate(lines) if _MARKER_RE.match(line)), None)
    if marker_at is None:
        known = set(known_projects or ())
        first = lines[0].lstrip("#").strip()
        if known and len(lines) >= MIN_BRIEF_LINES and project_key(first) in known:
            return _clean_label(first) or None
        return None
    if marker_at == 0:
        return "SAIHANDOFF"
    # The label is the line right above the marker, so code-block chrome an
    # older capture may carry above it ("text", "Copy code") is never the name.
    label = lines[marker_at - 1]
    # A project label is short: one to three words, not a sentence.
    if len(label) <= MAX_LABEL_LENGTH and len(label.split()) <= 3 and not label.endswith((".", ":", "?", "!")):
        return _clean_label(label) or "SAIHANDOFF"
    return "SAIHANDOFF"


def drop_filename(label: str, when: datetime.datetime | None = None) -> str:
    when = when or datetime.datetime.now()
    stem = _clean_label(label) or "SAIHANDOFF"
    return f"{stem}_{when:%Y%m%d_%H%M}.md"


def _read_index(folder: Path) -> dict[str, str]:
    try:
        data = json.loads((folder / INDEX_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if isinstance(v, str)}


def _write_index(folder: Path, index: dict[str, str]) -> None:
    if len(index) > INDEX_MAX_ENTRIES:
        index = dict(list(index.items())[-INDEX_MAX_ENTRIES:])
    try:
        atomic_write(folder / INDEX_NAME, json.dumps(index, ensure_ascii=False, indent=0))
    except OSError:
        # The index only saves a rewrite; losing it never loses a handoff.
        pass


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def materialize(
    text: str,
    label: str,
    folder: Path | str,
    when: datetime.datetime | None = None,
) -> tuple[Path, bool]:
    """Write ``text`` as a handoff file; return ``(path, reused)``.

    The same content always resolves to the file already written for it while
    that file still holds exactly these bytes. A new name never overwrites an
    existing file: a same-minute collision gets ``_2``, ``_3``, ...
    """
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    digest = _digest(text)
    index = _read_index(folder)
    known = index.get(digest)
    if known:
        candidate = folder / known
        try:
            if candidate.is_file() and _digest(candidate.read_text(encoding="utf-8")) == digest:
                return candidate, True
        except (OSError, UnicodeDecodeError):
            pass
    path = folder / drop_filename(label, when)
    base, suffix = path.with_suffix(""), path.suffix
    n = 2
    while path.exists():
        path = Path(f"{base}_{n}{suffix}")
        n += 1
    atomic_write(path, text)
    if _digest(path.read_text(encoding="utf-8")) != digest:
        raise OSError(f"handoff verification failed: {path.name}")
    index.pop(digest, None)
    index[digest] = path.name
    _write_index(folder, index)
    return path, False
