"""Read-only view of what the agent has actually done with a delivered audit.

AUDAPACK finishes an audit and the operator's next question is always the same:
has the agent read this one yet, or am I about to hand it the same work twice?
Nothing in AUDAPACK could answer it -- the audit left the station and went dark.

SAIPEN answers it, and durably. Its Audit Inbox (`SOURCE-AUDIT-INBOX-01`) treats
`<project>/audit/` as a transport inbox whose canonical layers are DIRECT files
matching ``^[1-9][0-9]*\\.md$`` -- nothing else is ever read, captured or
deleted. What it captured is journaled in
``<project>/.saipen/intake/audit_inbox.json``, keyed by ``audit/<N>.md``, and
each record carries the exact bytes it bound (``file_sha256``), the receipt, the
Work item it became, and the transport state.

This module reads those two facts and nothing else. It imports no SAIPEN code,
writes nothing anywhere, and never needs SAIPEN installed: a project without the
binding simply reads as never-consumed. Identity is content, never mtime --
copy, checkout and restore all move mtime without changing meaning, so a layer
whose bytes do not match its record is a NEW generation the agent has not seen.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

#: The canonical inbox directory, fixed by SOURCE-AUDIT-INBOX-01.
AUDIT_DIRNAME = "audit"
#: A canonical layer. `01.md`, `notes.md`, `1.txt` and `done/1.md` are not.
LAYER_RE = re.compile(r"^[1-9][0-9]*\.md$")
#: Where SAIPEN journals what it captured, relative to the project root.
BINDING_REL = ".saipen/intake/audit_inbox.json"
#: Dot-prefixed entries are directory infrastructure (`.gitkeep`, our own
#: `.gitignore`), never a producer's leftovers, so they are not residue.
RESIDUE_EXEMPT_PREFIX = "."
RESIDUE_REPORT_CAP = 20

# What AUDAPACK tells the operator. Ordered by urgency: the aggregate verdict
# for a project is the most urgent verdict any of its layers carries.
NO_INBOX = "NO_INBOX"
EMPTY = "EMPTY"
CONSUMED = "CONSUMED"
IN_WORK = "IN_WORK"
UNREAD = "UNREAD"
BLOCKED = "BLOCKED"

_VERDICT_URGENCY = {
    NO_INBOX: 0,
    EMPTY: 1,
    CONSUMED: 2,
    IN_WORK: 3,
    UNREAD: 4,
    BLOCKED: 5,
}

#: Transport states SAIPEN journals. Kept as data, not imported.
_STATE_VERDICT = {
    "NEW": UNREAD,
    "ACTIVE": IN_WORK,
    "BLOCKED": BLOCKED,
    "CLOSED_PENDING_DELETE": CONSUMED,
    "DELETED": CONSUMED,
    "INVALID": BLOCKED,
    "MISSING_AFTER_CAPTURE": BLOCKED,
}

_LABELS = {
    NO_INBOX: "—",
    EMPTY: "INBOX EMPTY",
    CONSUMED: "AGENT DONE",
    IN_WORK: "AGENT WORKING",
    UNREAD: "AGENT UNREAD",
    BLOCKED: "AGENT BLOCKED",
}

#: The one sentence the operator actually wants: run a new audit, or not.
_GUIDANCE = {
    NO_INBOX: "No audit/ inbox in this project. Enable the audit mirror to deliver one.",
    EMPTY: "Inbox is clean. A new audit is the useful thing to run.",
    CONSUMED: "The agent closed every delivered layer. A new audit is the useful thing to run.",
    IN_WORK: "The agent is working this audit now. Running a new one duplicates the work.",
    UNREAD: "Delivered and never read. Point the agent at it (cc) instead of auditing again.",
    BLOCKED: "The agent could not settle this layer. Read the reason before delivering more.",
}


@dataclass
class InboxLayer:
    """One canonical layer and what the agent did with it."""

    rel: str
    layer: int
    verdict: str
    sha256: str = ""
    size_bytes: int = 0
    present: bool = True
    generation: int = 1
    receipt_id: str = ""
    linked_work: str = ""
    detail: str = ""


@dataclass
class InboxState:
    """The whole answer for one project."""

    root: str = ""
    verdict: str = NO_INBOX
    layers: list[InboxLayer] = field(default_factory=list)
    residue: list[str] = field(default_factory=list)
    residue_truncated: bool = False
    has_binding: bool = False

    @property
    def label(self) -> str:
        return _LABELS.get(self.verdict, self.verdict)

    @property
    def guidance(self) -> str:
        return _GUIDANCE.get(self.verdict, "")

    @property
    def unread_count(self) -> int:
        return sum(1 for item in self.layers if item.verdict == UNREAD)

    @property
    def live_count(self) -> int:
        """Layers still physically in the inbox."""
        return sum(1 for item in self.layers if item.present)

    @property
    def wants_new_audit(self) -> bool:
        """True only when nothing delivered is still waiting on the agent."""
        return self.verdict in (EMPTY, CONSUMED)

    def summary(self) -> str:
        if self.verdict == UNREAD:
            return f"{self.label} · {self.unread_count}"
        if self.verdict == IN_WORK:
            work = next((item.linked_work for item in self.layers if item.linked_work), "")
            return f"{self.label} · {work}" if work else self.label
        return self.label


def audit_dir(root: Path | str) -> Path:
    return Path(str(root)) / AUDIT_DIRNAME


def layer_number(name: str) -> Optional[int]:
    """The positive layer number of a canonical filename, else None."""
    if not LAYER_RE.fullmatch(name):
        return None
    return int(name[:-3])


def next_layer_number(root: Path | str, directory: Path | str | None = None) -> int:
    """The lowest free canonical layer number, never overwriting a live one.

    Writing over an existing layer is legal in SAIPEN -- same path, changed
    bytes is simply a new generation -- but it yanks the document out from
    under an agent that may be working it right now. Allocating forward costs
    nothing and cannot do that.

    ``directory`` overrides the canonical ``<root>/audit`` for an operator who
    renamed the delivery folder; the binding is still consulted, because a
    number a settled receipt already used must not be handed out again.
    """
    highest = 0
    directory = Path(str(directory)) if directory is not None else audit_dir(root)
    try:
        entries = list(directory.iterdir())
    except OSError:
        return 1
    for entry in entries:
        number = layer_number(entry.name)
        if number is not None and entry.is_file():
            highest = max(highest, number)
    # A binding record for a layer already deleted still consumes its number:
    # reusing it would rebind fresh bytes onto a settled receipt's path.
    for rel in (_read_binding(root).get("layers") or {}):
        name = str(rel).rsplit("/", 1)[-1]
        number = layer_number(name)
        if number is not None:
            highest = max(highest, number)
    return highest + 1


def _read_binding(root: Path | str) -> dict[str, Any]:
    path = Path(str(root)) / BINDING_REL
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _digest(path: Path) -> tuple[str, int]:
    raw = path.read_bytes()
    return hashlib.sha256(raw).hexdigest(), len(raw)


def scan_residue(root: Path | str) -> tuple[list[str], bool]:
    """Everything in `audit/` SAIPEN will never read, capture or delete.

    Reported because a settled inbox holding leftovers is not the clean
    directory a green verdict would imply.
    """
    names: list[str] = []
    try:
        entries = sorted(audit_dir(root).iterdir(), key=lambda item: item.name)
    except OSError:
        return [], False
    for entry in entries:
        if entry.name.startswith(RESIDUE_EXEMPT_PREFIX):
            continue
        if entry.is_file() and layer_number(entry.name) is not None:
            continue
        names.append(entry.name)
    return names[:RESIDUE_REPORT_CAP], len(names) > RESIDUE_REPORT_CAP


def read_inbox(root: Path | str) -> InboxState:
    """What the agent has done with everything delivered to this project."""
    root_path = Path(str(root or ""))
    state = InboxState(root=str(root_path))
    if not str(root or "").strip() or not root_path.is_dir():
        return state
    directory = audit_dir(root_path)
    if not directory.is_dir():
        return state

    binding = _read_binding(root_path)
    bound = binding.get("layers") if isinstance(binding.get("layers"), dict) else {}
    state.has_binding = bool(bound)
    seen: set[str] = set()

    try:
        entries = sorted(directory.iterdir(), key=lambda item: item.name)
    except OSError:
        entries = []
    for entry in entries:
        number = layer_number(entry.name)
        if number is None or not entry.is_file():
            continue
        rel = f"{AUDIT_DIRNAME}/{entry.name}"
        seen.add(rel)
        try:
            digest, size = _digest(entry)
        except OSError:
            state.layers.append(InboxLayer(
                rel=rel, layer=number, verdict=BLOCKED, detail="layer is unreadable",
            ))
            continue
        record = bound.get(rel) if isinstance(bound.get(rel), dict) else None
        item = InboxLayer(rel=rel, layer=number, verdict=UNREAD, sha256=digest, size_bytes=size)
        if record and str(record.get("file_sha256") or "") == digest:
            item.generation = int(record.get("generation") or 1)
            item.receipt_id = str(record.get("receipt_id") or "")
            item.linked_work = str(record.get("linked_work") or "")
            item.verdict = _STATE_VERDICT.get(str(record.get("state") or ""), UNREAD)
        elif record:
            # Same path, different bytes. Whatever the old generation settled
            # to, these bytes are new and nobody has read them.
            item.generation = int(record.get("generation") or 1) + 1
            item.detail = "rewritten since the agent captured it"
        state.layers.append(item)

    # Records whose file is gone: closed ones are the normal end of the
    # lifecycle, anything else vanished while still owed.
    for rel, record in sorted(bound.items()):
        if rel in seen or not isinstance(record, dict):
            continue
        raw_state = str(record.get("state") or "")
        settled = raw_state in ("DELETED", "CLOSED_PENDING_DELETE")
        name = str(rel).rsplit("/", 1)[-1]
        state.layers.append(InboxLayer(
            rel=str(rel),
            layer=layer_number(name) or int(record.get("layer") or 0),
            verdict=CONSUMED if settled else BLOCKED,
            sha256=str(record.get("file_sha256") or ""),
            present=False,
            receipt_id=str(record.get("receipt_id") or ""),
            linked_work=str(record.get("linked_work") or ""),
            detail="" if settled else "captured layer is no longer in the inbox",
        ))

    state.residue, state.residue_truncated = scan_residue(root_path)
    if state.layers:
        state.verdict = max(
            (item.verdict for item in state.layers),
            key=lambda verdict: _VERDICT_URGENCY.get(verdict, 0),
        )
    else:
        state.verdict = EMPTY
    return state


_CACHE: dict[str, tuple[float, tuple, InboxState]] = {}
_CACHE_TTL_SECONDS = 8.0


def _inbox_fingerprint(root: Path | str) -> tuple:
    """Name, size and mtime of every inbox entry, plus the binding's own stamp.

    Not the directory's own mtime: on Windows that is too coarse to notice a
    layer arriving in the same tick, which is exactly when a fresh delivery
    must become visible.
    """
    entries: list[tuple[str, int, int]] = []
    try:
        for item in sorted(audit_dir(root).iterdir(), key=lambda entry: entry.name):
            try:
                stat = item.stat()
                entries.append((item.name, int(stat.st_size), int(stat.st_mtime_ns)))
            except OSError:
                entries.append((item.name, -1, -1))
    except OSError:
        pass
    try:
        binding = (Path(str(root)) / BINDING_REL).stat()
        entries.append((BINDING_REL, int(binding.st_size), int(binding.st_mtime_ns)))
    except OSError:
        pass
    return tuple(entries)


def read_inbox_cached(root: Path | str, now: Optional[float] = None) -> InboxState:
    """``read_inbox`` with a bounded cache for repeated dashboard refreshes.

    A dashboard reads this every few seconds for every project and each read
    hashes the delivered layers. The cheap fingerprint above decides; the TTL
    only bounds how long a stale answer can survive a filesystem that reports
    no change at all.
    """
    import time

    key = str(root or "")
    if not key:
        return InboxState()
    stamp = float(now if now is not None else time.time())
    fingerprint = _inbox_fingerprint(key)
    hit = _CACHE.get(key)
    if hit and hit[1] == fingerprint and stamp - hit[0] < _CACHE_TTL_SECONDS:
        return hit[2]
    state = read_inbox(key)
    _CACHE[key] = (stamp, fingerprint, state)
    return state
