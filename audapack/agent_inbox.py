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
#: Default probe location for the agent's own record of what it captured,
#: relative to the project root. A default, not a hardcode: callers pass their
#: own, and a project with nothing there simply reads as never-consumed.
DEFAULT_BINDING_REL = ".saipen/intake/audit_inbox.json"
#: Kept as the module-level default so existing callers need not pass a path.
BINDING_REL = DEFAULT_BINDING_REL
#: Some agents reserve a layer number BEFORE the bytes land, so a number can be
#: durably spent while existing on neither disk nor in the journal. Probing the
#: allocator too is the difference between allocating forward and colliding.
DEFAULT_ALLOCATOR_REL = ".saipen/intake/audit_allocator.json"
#: The journal shape this reader was written against. A different value means
#: the contract moved and these records must not be parsed as if it had not --
#: that is the one field that keeps a copied contract honest instead of
#: silently drifting into confident wrong answers.
SUPPORTED_SCHEMA_VERSION = 1
#: Dot-prefixed entries are directory infrastructure (`.gitkeep`, our own
#: `.gitignore`), never a producer's leftovers, so they are not residue.
RESIDUE_EXEMPT_PREFIX = "."
RESIDUE_REPORT_CAP = 20

# What AUDAPACK tells the operator. Ordered by urgency: the aggregate verdict
# for a project is the most urgent verdict any of its layers carries.
NO_INBOX = "NO_INBOX"
EMPTY = "EMPTY"
CONSUMED = "CONSUMED"
UNKNOWN = "UNKNOWN"
IN_WORK = "IN_WORK"
UNREAD = "UNREAD"
BLOCKED = "BLOCKED"

# The headline is the ACTION, so the ranking is by what the operator should do
# and NOT by how alarming a layer looks. A blocked layer is a diagnostic: an
# unreadable file, or a record whose transport vanished after capture -- and a
# vanished transport is never a reason to lose Work, because the receipt is
# already durable authority. Ranking it top hid a fresh unread audit sitting
# right beside it (__SAITULS: orphan record for a gone audit/1.md, unread 2.md
# on disk, headline "AGENT BLOCKED · read the reason before delivering more").
# The agent pins the inverse rule with a test: an invalid lower layer never
# starves a later workable one. Blocked stays visible, as a note, not a
# headline.
_VERDICT_URGENCY = {
    NO_INBOX: 0,
    EMPTY: 1,
    CONSUMED: 2,
    BLOCKED: 3,
    IN_WORK: 4,
    UNREAD: 5,
    UNKNOWN: 6,
}

#: Transport states the agent journals. Kept as data, not imported.
#: ACTIVE is deliberately absent: the state field alone does not say whether
#: anyone is working it -- see _verdict_for_record.
_STATE_VERDICT = {
    "NEW": UNREAD,
    "BLOCKED": BLOCKED,
    "CLOSED_PENDING_DELETE": CONSUMED,
    "DELETED": CONSUMED,
    "INVALID": BLOCKED,
    "MISSING_AFTER_CAPTURE": BLOCKED,
}


def _verdict_for_record(record: dict[str, Any]) -> str:
    """What a bound layer means, from state AND the Work it became.

    An ACTIVE record with no linked_work was captured into a receipt that never
    became a ticket. Nobody is working it -- it is stalled and still owed, and
    the agent's own projection answers `audit ingest` on exactly this state.
    Reading the state field alone told the operator to wait for a worker that
    does not exist (_SAIWORK2: audit/1.md ACTIVE, SRC-002, linked_work null).
    """
    state = str(record.get("state") or "")
    if state == "ACTIVE":
        return IN_WORK if str(record.get("linked_work") or "").strip() else UNREAD
    return _STATE_VERDICT.get(state, UNREAD)

_LABELS = {
    NO_INBOX: "—",
    EMPTY: "INBOX EMPTY",
    CONSUMED: "AGENT DONE",
    IN_WORK: "AGENT WORKING",
    UNREAD: "AGENT UNREAD",
    BLOCKED: "AGENT BLOCKED",
    UNKNOWN: "AGENT STATE UNREADABLE",
}

#: The one sentence the operator actually wants: run a new audit, or not.
_GUIDANCE = {
    NO_INBOX: "No audit/ inbox in this project. Enable the audit mirror to deliver one.",  # noqa: E501
    EMPTY: "Inbox is clean. A new audit is the useful thing to run.",
    CONSUMED: "The agent closed every delivered layer. A new audit is the useful thing to run.",
    IN_WORK: "The agent is working this audit now. Running a new one duplicates the work.",
    UNREAD: "Delivered and never read. Point the agent at it (cc) instead of auditing again.",
    BLOCKED: "The agent could not settle this layer. Read the reason before delivering more.",
    UNKNOWN: "The agent's journal is a schema this build does not read. Treat its state as unknown.",
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
    def unread_count(self) -> int:
        return sum(1 for item in self.layers if item.verdict == UNREAD)

    @property
    def blocked_count(self) -> int:
        """Diagnostics. They are reported, and they never become the headline."""
        return sum(1 for item in self.layers if item.verdict == BLOCKED)

    @property
    def stalled_count(self) -> int:
        """Unread layers the agent already captured into a receipt with no Work."""
        return sum(1 for item in self.layers if item.verdict == UNREAD and item.receipt_id)

    @property
    def live_count(self) -> int:
        """Layers still physically in the inbox."""
        return sum(1 for item in self.layers if item.present)

    @property
    def wants_new_audit(self) -> bool:
        """True only when nothing delivered is still waiting on the agent.

        Residue counts against it. The inbox rule is REPORT_NEVER_DELETE, so
        leftovers are permanent until someone removes them, and an inbox
        holding them is not the clean directory a green verdict implies --
        the agent's own status answers `clean: false` on exactly this state.
        """
        return self.verdict in (EMPTY, CONSUMED) and not self.residue

    @property
    def guidance(self) -> str:
        base = _GUIDANCE.get(self.verdict, "")
        if self.verdict == UNREAD and self.stalled_count:
            receipts = ", ".join(
                item.receipt_id for item in self.layers
                if item.verdict == UNREAD and item.receipt_id
            )
            base = (
                f"Captured as {receipts} but never turned into work"
                f" ({self.stalled_count} of {self.unread_count}). Hand it back (cc);"
                " nobody is working it."
            )
        if self.blocked_count and self.verdict != BLOCKED:
            base = f"{base} {self.blocked_count} earlier layer(s) unsettled -- a note, not a blocker."
        if not self.residue:
            return base
        shown = ", ".join(self.residue[:3])
        more = "..." if len(self.residue) > 3 or self.residue_truncated else ""
        note = (
            f"{len(self.residue)}{'+' if self.residue_truncated else ''} file(s) the agent never reads"
            f" and never deletes: {shown}{more}."
        )
        if self.verdict in (EMPTY, CONSUMED):
            # Never say "clean" while leftovers are sitting there.
            return f"Nothing is owed, but the inbox is not clean. {note}"
        return f"{base} {note}"

    def summary(self) -> str:
        residue = f" · +{len(self.residue)} residue" if self.residue else ""
        # A blocked layer is reported beside the headline, never as it.
        if self.blocked_count and self.verdict != BLOCKED:
            residue = f" · !{self.blocked_count} blocked{residue}"
        if self.verdict == UNREAD:
            return f"{self.label} · {self.unread_count}{residue}"
        if self.verdict == IN_WORK:
            work = next((item.linked_work for item in self.layers if item.linked_work), "")
            head = f"{self.label} · {work}" if work else self.label
            return f"{head}{residue}"
        return f"{self.label}{residue}"


def audit_dir(root: Path | str) -> Path:
    return Path(str(root)) / AUDIT_DIRNAME


def layer_number(name: str) -> Optional[int]:
    """The positive layer number of a canonical filename, else None."""
    if not LAYER_RE.fullmatch(name):
        return None
    return int(name[:-3])


def next_layer_number(root: Path | str, directory: Path | str | None = None,
                      binding_rel: str = "", allocator_rel: str = "") -> int:
    """The lowest free canonical layer number, never overwriting a live one.

    Writing over an existing layer is legal in SAIPEN -- same path, changed
    bytes is simply a new generation -- but it yanks the document out from
    under an agent that may be working it right now. Allocating forward costs
    nothing and cannot do that.

    ``directory`` overrides the canonical ``<root>/audit`` for an operator who
    renamed the delivery folder; the journals are still consulted, because a
    number already spent must not be handed out again.

    THREE floors, not two. Disk and the capture journal both only know numbers
    whose bytes exist. An agent that reserves an id before the bytes land has
    spent numbers that appear in neither -- SAIPEN's allocator held
    ``next_id: 5`` with layer 4 committed while disk and binding topped out at
    3, so a two-source floor would have handed out 4 and keyed two different
    audits on the same ``audit/4.md`` in provenance records.
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
    for rel in (_read_binding(root, binding_rel).get("layers") or {}):
        name = str(rel).rsplit("/", 1)[-1]
        number = layer_number(name)
        if number is not None:
            highest = max(highest, number)
    # Reserved-but-unplaced ids live only here.
    allocator = _read_allocator(root, allocator_rel)
    try:
        highest = max(highest, int(allocator.get("next_id") or 0) - 1)
    except (TypeError, ValueError):
        pass
    operations = allocator.get("operations")
    if isinstance(operations, dict):
        for record in operations.values():
            if not isinstance(record, dict):
                continue
            try:
                highest = max(highest, int(record.get("layer") or 0))
            except (TypeError, ValueError):
                continue
    return highest + 1


def _read_journal(root: Path | str, rel: str, default_rel: str) -> tuple[dict[str, Any], bool]:
    """One agent journal, or ``({}, False)`` when it must not be parsed.

    The boolean is "readable": absent is fine and simply contributes nothing,
    but a journal whose ``schema_version`` is not the one this reader was
    written against is NOT fine. Parsing it anyway would answer confidently
    from a contract that moved. Callers turn that into UNKNOWN rather than a
    guess.
    """
    path = Path(str(root)) / (str(rel or "").strip() or default_rel)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}, True
    try:
        data = json.loads(raw)
    except ValueError:
        return {}, False
    if not isinstance(data, dict):
        return {}, False
    if int(data.get("schema_version") or 0) != SUPPORTED_SCHEMA_VERSION:
        return {}, False
    return data, True


def _read_binding(root: Path | str, binding_rel: str = "") -> dict[str, Any]:
    return _read_journal(root, binding_rel, BINDING_REL)[0]


def _read_allocator(root: Path | str, allocator_rel: str = "") -> dict[str, Any]:
    return _read_journal(root, allocator_rel, DEFAULT_ALLOCATOR_REL)[0]


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
        # Name decides. A directory or symlink called `1.md` is a BAD LAYER,
        # not residue -- calling it residue would have this reader and the
        # agent disagree about the same entry.
        if layer_number(entry.name) is not None:
            continue
        names.append(entry.name)
    return names[:RESIDUE_REPORT_CAP], len(names) > RESIDUE_REPORT_CAP


def read_inbox(root: Path | str, binding_rel: str = "") -> InboxState:
    """What the agent has done with everything delivered to this project."""
    root_path = Path(str(root or ""))
    state = InboxState(root=str(root_path))
    if not str(root or "").strip() or not root_path.is_dir():
        return state
    directory = audit_dir(root_path)
    if not directory.is_dir():
        return state

    binding, readable = _read_journal(root_path, binding_rel, BINDING_REL)
    if not readable:
        state.verdict = UNKNOWN
        state.residue, state.residue_truncated = scan_residue(root_path)
        return state
    bound = binding.get("layers") if isinstance(binding.get("layers"), dict) else {}
    state.has_binding = bool(bound)
    seen: set[str] = set()

    try:
        entries = sorted(directory.iterdir(), key=lambda item: item.name)
    except OSError:
        entries = []
    for entry in entries:
        number = layer_number(entry.name)
        if number is None:
            continue
        rel = f"{AUDIT_DIRNAME}/{entry.name}"
        seen.add(rel)
        if not entry.is_file():
            state.layers.append(InboxLayer(
                rel=rel, layer=number, verdict=BLOCKED,
                detail="canonical layer name is not a regular file",
            ))
            continue
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
            item.verdict = _verdict_for_record(record)
            if item.verdict == UNREAD and item.receipt_id:
                item.detail = f"captured as {item.receipt_id} but never became work"
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


_CACHE: dict[tuple[str, str], tuple[float, tuple, InboxState]] = {}
_CACHE_TTL_SECONDS = 8.0
#: PERF-004 (audit/10.md): the module cache used to grow forever. A project
#: removed or moved left its root+binding key behind indefinitely. Bound it
#: deterministically: insertion-ordered oldest-first eviction past this many
#: distinct keys. Every entry can be rebuilt from the filesystem, so eviction
#: is always safe.
_CACHE_MAX_ENTRIES = 512

#: PERF-004: the passive dashboard may reuse a settled verdict inside this
#: budget instead of re-scanning the inbox on every repaint. The active
#: dashboard cadence is ``MainWindow.BRIDGE_POLL_ACTIVE_MS = 4000`` and Qt
#: timers fire at or AFTER their interval, so a budget equal to that interval
#: expires on every active repaint and buys nothing. The budget must exceed the
#: poll by a meaningful margin: at 8 seconds one normal 4-second repaint is
#: served from zero-I/O passive state while a genuine inbox change is still
#: noticed within the next budget window, and expiry still performs exactly one
#: fresh physical probe. Do not set this to the caller's poll interval.
_PASSIVE_FRESHNESS_SECONDS = 8.0
#: Per-key state for the passive layer: (probe_stamp, state). No fingerprint:
#: inside the budget nothing is read; on expiry a full physical probe runs, so
#: there is no cheap-fingerprint middle path to justify.
_PASSIVE_CACHE: dict[tuple[str, str], tuple[float, InboxState]] = {}


def _inbox_fingerprint(root: Path | str, binding_rel: str = "") -> tuple:
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
        rel = str(binding_rel or "").strip() or BINDING_REL
        binding = (Path(str(root)) / rel).stat()
        entries.append((rel, int(binding.st_size), int(binding.st_mtime_ns)))
    except OSError:
        pass
    return tuple(entries)


def _cache_store(cache: dict, key: tuple[str, str], value: object) -> None:
    """Insert with deterministic oldest-first eviction past the bound."""
    cache[key] = value
    while len(cache) > _CACHE_MAX_ENTRIES:
        cache.pop(next(iter(cache)))


def invalidate_inbox_cache(root: Path | str, binding_rel: str = "") -> None:
    """Drop the cached verdict for one project after a known inbox change.

    Called by a producer that just wrote a layer or a binding, so the next
    passive repaint forces a physical probe instead of reusing a verdict that
    the change just made stale.
    """
    key = (str(root), str(binding_rel or ""))
    _CACHE.pop(key, None)
    _PASSIVE_CACHE.pop(key, None)


def read_inbox_cached(root: Path | str, now: Optional[float] = None,
                      binding_rel: str = "") -> InboxState:
    """``read_inbox`` with a bounded cache for repeated dashboard refreshes.

    A dashboard reads this every few seconds for every project and each read
    hashes the delivered layers. The cheap fingerprint above decides; the TTL
    only bounds how long a stale answer can survive a filesystem that reports
    no change at all.

    AUTHORITATIVE-ON-CHANGE: this fingerprints on EVERY call, so a same-instant
    delivery is visible immediately (``test_the_cache_follows_the_inbox_rather_
    than_a_clock``). Callers whose correctness depends on current truth use
    this. The passive dashboard layer is ``read_inbox_passive``.
    """
    import time

    if not str(root or ""):
        return InboxState()
    key = (str(root), str(binding_rel or ""))
    stamp = float(now if now is not None else time.time())
    fingerprint = _inbox_fingerprint(key[0], binding_rel)
    hit = _CACHE.get(key)
    if hit and hit[1] == fingerprint and stamp - hit[0] < _CACHE_TTL_SECONDS:
        return hit[2]
    state = read_inbox(key[0], binding_rel)
    _cache_store(_CACHE, key, (stamp, fingerprint, state))
    return state


def read_inbox_passive(root: Path | str, now: Optional[float] = None,
                       binding_rel: str = "",
                       freshness_seconds: float = _PASSIVE_FRESHNESS_SECONDS,
                       force: bool = False) -> InboxState:
    """Dashboard repaint path: reuse a settled verdict inside its freshness budget.

    PERF-004: ``read_inbox_cached`` fingerprints the whole inbox on every call,
    so a live dashboard polling every 4 seconds re-enumerates and stats every
    settled project even though its advertised TTL is 8 seconds. Moving the TTL
    ahead of the fingerprint would break the same-instant delivery contract, so
    the passive layer is separate instead:

    * a repaint INSIDE the freshness budget returns the last settled verdict
      with ZERO filesystem work;
    * the budget's expiry, ``force``, or a missing verdict runs one full
      ``read_inbox`` and refreshes the passive record, so an invalidation or a
      decision-critical read is never answered from a stale passive snapshot.

    Callers that need authoritative current truth keep calling
    ``read_inbox_cached``; operators force a probe before any action whose
    correctness depends on the inbox verdict.

    The freshness age is process-local elapsed time, so the default clock is
    ``time.monotonic()``: a wall-clock correction must neither expire every
    passive entry at once nor extend a stale verdict after a rollback. Tests
    inject an explicit ``now`` and keep deterministic behaviour.
    """
    import time

    if not str(root or ""):
        return InboxState()
    key = (str(root), str(binding_rel or ""))
    stamp = float(now if now is not None else time.monotonic())
    hit = _PASSIVE_CACHE.get(key)
    if not force and hit is not None and stamp - hit[0] < max(0.0, float(freshness_seconds)):
        return hit[1]
    state = read_inbox(key[0], binding_rel)
    _cache_store(_PASSIVE_CACHE, key, (stamp, state))
    return state
