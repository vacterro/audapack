from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import uuid
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from audapack.config import cross_process_lock, get_user_runtime_dir
from audapack.models import Project
from audapack.saipen_transport import is_managed

logger = logging.getLogger(__name__)

INAUDIT_RE = re.compile(r"^[1-9][0-9]*\.md$")

#: Bookkeeping file per project for layers the OPERATOR created through the
#: desktop app (the [+] button / ensure_next_layer). Widget-delivered layers
#: come from inaudit_capture.assign or the Bridge and are deliberately NEVER
#: recorded here, so the row [edit-last-custom] button can never land on an
#: AUDAPACK-widget layer. Kept OUTSIDE the audit dir: a sidecar inside it would
#: be packed into archives and read by the Agent Inbox residue scan.
_USER_LAYER_LOCK = "inaudit_user_layers.lock"

#: Runtime storage roots (CORE-002/CORE-003). Never inside audit/: agents
#: consume that directory and must only ever see canonical numbered layers.
_MUTATION_LOCK_PREFIX = "inaudit_mutate-"
_REORDER_DIR = "inaudit_reorder"


def _project_digest(project: Project) -> str:
    return hashlib.sha256(str(project.id).encode("utf-8")).hexdigest()[:24]


def project_mutation_lock(project: Project):
    """One project-scoped mutation lane for Create/Delete/Rename/Reorder.

    Serializes every unmanaged numbered-layer mutation of one project across
    processes. The lock file lives in the runtime dir, never inside audit/
    where an agent might consume it.
    """
    if not project or not project.id:
        return nullcontext()
    lock_path = get_user_runtime_dir() / f"{_MUTATION_LOCK_PREFIX}{_project_digest(project)}.lock"
    return cross_process_lock(lock_path)


def user_layer_registry_path(project: Project) -> Optional[Path]:
    if not project or not project.id:
        return None
    digest = hashlib.sha256(str(project.id).encode("utf-8")).hexdigest()[:24]
    return get_user_runtime_dir() / "inaudit_user_layers" / f"{digest}.json"


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data).encode("utf-8")
    fd, temp_name = tempfile.mkstemp(prefix=".inauditjson-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _load_user_layers(path: Optional[Path]) -> list[int]:
    if path is None:
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        numbers = data.get("user_layers") if isinstance(data, dict) else None
        return [int(value) for value in (numbers or []) if str(value).isdigit()]
    except (OSError, ValueError):
        return []


def _save_user_layers(path: Optional[Path], numbers: list[int]) -> None:
    if path is None:
        return
    _atomic_write_json(path, {"user_layers": numbers, "schema_version": 1})


def _edit_user_layers(project: Project, mutate) -> None:
    path = user_layer_registry_path(project)
    lock_path = get_user_runtime_dir() / _USER_LAYER_LOCK
    with cross_process_lock(lock_path):
        numbers = _load_user_layers(path)
        mutate(numbers)
        _save_user_layers(path, numbers)


def record_user_layer(project: Project, number: int) -> str:
    """Mark a layer as operator-created (secondary metadata, CORE-002).

    Returns "" on success or a short repair-pending warning. NEVER raises to
    the caller of a primary operation: a failed tracker write is recorded as
    a durable repair intent instead.
    """
    return _tracked_update(project, lambda nums: _apply_record(nums, number), {"op": "record", "number": int(number)})


def forget_user_layer(project: Project, number: int) -> str:
    """Forget an operator-created layer. Idempotent; never raises."""
    return _tracked_update(project, lambda nums: _apply_forget(nums, int(number)), {"op": "forget", "number": int(number)})


def renumber_user_layer(project: Project, old: int, new: int) -> str:
    """The layer moved numbers (rename / reorder); the marker follows it.

    Only ``old`` is rewritten: a tracked entry for ``new`` either belongs to
    this same dance (the park+place reorder always rewrites it away first) or
    is a stale record for a file that is already gone, which the existence
    check in last_user_layer skips anyway.
    """
    return _tracked_update(
        project,
        lambda nums: _apply_renumber(nums, int(old), int(new)),
        {"op": "renumber", "old": int(old), "new": int(new)},
    )


def last_user_layer(project: Project) -> Optional[int]:
    """The most recently created operator layer that still exists on disk.

    Deleted and consumed layers are skipped; the caller sees the newest live
    one, or None when the operator has never created one (or deleted them all).
    """
    path = user_layer_registry_path(project)
    d = inaudit_dir(project)
    if d is None:
        return None
    for number in reversed(_load_user_layers(path)):
        candidate = (d / f"{number}.md").resolve()
        try:
            if candidate.is_file() and candidate.relative_to(d.resolve()):
                return number
        except ValueError:
            continue
    return None


# ----------------------------------------------------------- tracker repair
# CORE-002: canonical numbered-layer mutation is PRIMARY truth; the user-layer
# tracker is SECONDARY metadata. When the tracker write fails after a committed
# canonical operation, the failure is recorded as a durable repair intent and
# replayed at the next safe boundary -- the canonical operation is NEVER
# reported as failed, and a retry never duplicates the layer.

_REPAIR_SCHEMA = {"schema_version": 1, "queue": []}

#: Warning surfaced next to a committed primary operation. Distinct so the
#: operator is never told a durable repair exists when the queue write itself
#: failed (CORE-002 B2/B3).
TRACKING_REPAIR_PENDING = "user-layer tracking repair pending"
TRACKING_REPAIR_UNPERSISTED = (
    "user-layer tracking update failed and repair intent could not be persisted"
)


def _repair_queue_path(project: Project) -> Path:
    return get_user_runtime_dir() / "inaudit_user_layers" / f"repair-{_project_digest(project)}.json"


def _append_repair_intent(project: Project, intent: dict) -> bool:
    """Durably enqueue one tracker repair intent.

    Returns True only when the intent is durably persisted. Never raises: the
    PRIMARY canonical operation has already committed and must not be rolled
    back because secondary metadata storage failed.
    """
    try:
        path = _repair_queue_path(project)
        path.parent.mkdir(parents=True, exist_ok=True)
        with cross_process_lock(get_user_runtime_dir() / _USER_LAYER_LOCK):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {"schema_version": 1, "queue": []}
            if not isinstance(data, dict) or not isinstance(data.get("queue"), list):
                data = {"schema_version": 1, "queue": []}
            data["queue"].append(intent)
            _atomic_write_json(path, data)
        return True
    except Exception:  # noqa: BLE001 -- queue persistence is best effort
        logger.warning("tracker repair intent could not be enqueued", exc_info=True)
        return False


def _canonical_has_layer(project: Project, number: int) -> bool:
    """Canonical postcondition evidence: does audit/<N>.md exist?"""
    p = resolve_inaudit_path(project, number)
    return bool(p and p.is_file())


def replay_tracker_repairs(project: Project) -> str:
    """Apply durable tracker repair intents against canonical truth.

    Every intent names an operation and enough postcondition evidence to
    decide whether the PRIMARY operation actually committed. Only then is
    the tracker updated; an intent whose postcondition is unprovable is
    dropped (the tracker can be rebuilt from future operations). Idempotent:
    replaying the same queue twice has one effect.
    """
    path = _repair_queue_path(project)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    queue = data.get("queue") if isinstance(data, dict) else None
    if not isinstance(queue, list) or not queue:
        return ""
    remaining: list[dict] = []
    pending = False
    # NOTE: no outer lock here -- _edit_user_layers takes the user-layer lock
    # per intent. A same-process re-entry on the same lock file would deadlock
    # (msvcrt.locking conflicts even within one process).
    for intent in queue:
        try:
            op = intent.get("op")
            if op == "record":
                number = int(intent.get("number", 0))
                if _canonical_has_layer(project, number):
                    _edit_user_layers(project, lambda nums, n=number: _apply_record(nums, n))
                # else: layer consumed post-commit; nothing to record.
            elif op == "forget":
                forget_number = int(intent.get("number", 0))
                _edit_user_layers(project, lambda nums, n=forget_number: _apply_forget(nums, n))
            elif op == "renumber":
                old = int(intent.get("old", 0))
                new = int(intent.get("new", 0))
                if _canonical_has_layer(project, new) or not _canonical_has_layer(project, old):
                    _edit_user_layers(
                        project, lambda nums, o=old, n=new: _apply_renumber(nums, o, n)
                    )
                else:
                    # Postcondition unproven: source still there, target
                    # absent -- a rename that never completed. Drop.
                    pass
            elif op == "renumber_map":
                # Reorder committed: renumber every tracked marker through
                # the final mapping. Canonical postcondition = the target
                # layer of each mover exists.
                mapping = intent.get("mapping") or {}
                renumbered = dict(
                    (int(k), int(v)) for k, v in mapping.items() if str(k).isdigit() and str(v).isdigit()
                )
                for old, new in sorted(renumbered.items()):
                    if _canonical_has_layer(project, new) or not _canonical_has_layer(project, old):
                        _edit_user_layers(
                            project, lambda nums, o=old, n=new: _apply_renumber(nums, o, n)
                        )
            else:
                pass
        except Exception:  # noqa: BLE001 -- one bad intent must not wedge the queue
            pending = True
            remaining.append(intent)
    try:
        _atomic_write_json(path, {"schema_version": 1, "queue": remaining})
    except OSError:
        return "tracker repair queue could not be persisted"
    return "tracker repair pending" if (remaining or pending) else ""


def _apply_record(nums: list[int], number: int) -> None:
    if number in nums:
        nums.remove(number)
    nums.append(number)


def _apply_forget(nums: list[int], number: int) -> None:
    try:
        nums.remove(number)
    except ValueError:
        pass


def _apply_renumber(nums: list[int], old: int, new: int) -> None:
    if old in nums:
        nums[:] = [new if value == old else value for value in nums]


def _tracked_update(project: Project, mutate, intent: dict) -> str:
    """Run one tracker mutation; on failure enqueue the durable repair intent.

    Returns "" on success. When the tracker write fails the return value tells
    the truth about the repair queue itself: a durably enqueued intent is
    ``TRACKING_REPAIR_PENDING``; an unpersistable intent is
    ``TRACKING_REPAIR_UNPERSISTED`` -- never a false "repair pending". The
    primary canonical operation is already committed either way.
    """
    try:
        _edit_user_layers(project, mutate)
        return ""
    except Exception:  # noqa: BLE001 -- secondary metadata never fails the primary op
        if _append_repair_intent(project, intent):
            return TRACKING_REPAIR_PENDING
        return TRACKING_REPAIR_UNPERSISTED

@dataclass
class InauditLayer:
    number: int
    path: Path
    size_bytes: int
    size_str: str

_selection: dict[str, int] = {}

def inaudit_dir(project: Project) -> Optional[Path]:
    if not project or not project.source_path:
        return None
    try:
        return Path(project.source_path) / "audit"
    except Exception:
        return None

def _human_size(n: int) -> str:
    if n == 0:
        return "0 B"
    if n < 1024:
        return f"{n} B"
    kb = n / 1024
    if kb < 1024:
        return f"{kb:.1f} KB"
    return f"{kb/1024:.1f} MB"

def recover_inaudit_state(project: Project) -> None:
    """Bounded first-safe-access recovery for one project (CORE-001/002/003).

    Resolves interrupted layer-save journals, pending reorder journals and
    durable tracker-repair intents. Never runs in a paint/data() path: the
    callers are project bind and each mutation entry.
    """
    d = inaudit_dir(project)
    if d is not None and d.is_dir():
        from audapack.inaudit_commit import recover_inaudit_dir_commits

        recover_inaudit_dir_commits(d)
    recover_pending_reorders(project)
    replay_tracker_repairs(project)


def list_inaudit_layers(project: Project) -> list[InauditLayer]:
    d = inaudit_dir(project)
    if d is None or not d.is_dir():
        return []
    layers: list[InauditLayer] = []
    try:
        for p in d.iterdir():
            if not p.is_file():
                continue
            if not INAUDIT_RE.match(p.name):
                continue
            try:
                num = int(p.stem)
                sz = p.stat().st_size
                layers.append(InauditLayer(number=num, path=p.resolve(), size_bytes=sz, size_str=_human_size(sz)))
            except (ValueError, OSError):
                # A non-numeric name is simply not a layer, and a file that
                # cannot be stat'd is not readable as one. Anything WIDER than
                # that would disguise a real bug in here as "no layers", which
                # renders as an empty inbox and no error anywhere.
                continue
    except OSError:
        # The directory went away or cannot be read: no layers is the honest
        # answer. Wider than that and a real bug in the scan renders as an
        # empty inbox with no error anywhere.
        return []
    layers.sort(key=lambda x: x.number)
    return layers

def inaudit_count(project: Project) -> int:
    return len(list_inaudit_layers(project))


def has_pending_inaudit(project: Project) -> bool:
    """True while at least one canonical layer is waiting to be consumed.

    A layer is the request; it is consumed by being read, not by starting
    another audit. Group dispatch uses this to avoid spending a browser window
    on work an operator already asked for. It is a POLICY input, never a
    refusal: a project with no layer is simply eligible.
    """
    return bool(list_inaudit_layers(project))

def get_inaudit_selected(project: Project) -> Optional[int]:
    if not project or not project.id:
        return None
    sel = _selection.get(str(project.id))
    layers = list_inaudit_layers(project)
    if not layers:
        return None
    if sel is not None and any(x.number == sel for x in layers):
        return sel
    return layers[0].number


def _raw_inaudit_selected(project: Project) -> Optional[int]:
    """The recorded selection WITHOUT the lowest-layer fallback.

    The follow-the-selection updates in rename/delete run after the source
    file is already gone, so the fallback answer there is whatever layer
    happens to sort first -- comparing against it silently skipped the
    update (the reorder dance left the selection pinned to a stale number).
    """
    if not project or not project.id:
        return None
    return _selection.get(str(project.id))

def set_inaudit_selected(project: Project, number: Optional[int]) -> None:
    if not project or not project.id:
        return
    if number is None:
        _selection.pop(str(project.id), None)
        return
    try:
        n = int(number)
    except (TypeError, ValueError):
        return
    if n >= 1:
        _selection[str(project.id)] = n

def get_active_inaudit_path(project: Project) -> Optional[Path]:
    sel = get_inaudit_selected(project)
    if sel is None:
        return None
    p = resolve_inaudit_path(project, sel)
    if p is None:
        return None
    return p if p.is_file() else None

def resolve_inaudit_path(project: Project, number: int) -> Optional[Path]:
    d = inaudit_dir(project)
    if d is None:
        return None
    try:
        n = int(number)
        if n < 1:
            return None
    except Exception:
        return None
    cand = (d / f"{n}.md").resolve()
    try:
        d.resolve().as_posix()
        cand.relative_to(d.resolve())
    except Exception:
        return None
    return cand

def open_exclusive_layer(path: Path) -> int:
    """Create ``path`` and return its write descriptor, or raise FileExistsError.

    The canonical ``audit/<N>.md`` namespace has several producers (this module,
    the capture store, the Bridge). Any check-then-write pair lets two of them
    pick the same free number and lets the loser truncate the winner's audit
    text, so creation of a numbered layer goes through this one primitive.
    """
    return os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0))


def reserve_next_layer(audit_dir: Path, start: int = 1) -> tuple[int, Path, int]:
    """Reserve the lowest free layer number >= ``start``.

    Returns ``(number, path, fd)``; the caller owns the open descriptor. A taken
    number is never opened for writing, so a competitor's layer cannot be
    emptied by a producer that merely scanned the directory a moment earlier.
    """
    number = max(1, int(start))
    while True:
        candidate = audit_dir / f"{number}.md"
        try:
            return number, candidate, open_exclusive_layer(candidate)
        except FileExistsError:
            number += 1


def ensure_next_layer(project: Project) -> Path:
    if project.source_path and is_managed(project.source_path):
        raise ValueError("SAIPEN layers need audit text; capture and assign through the Inbox")
    d = inaudit_dir(project)
    if d is None:
        # An assert here vanished under python -O and left d.mkdir raising
        # AttributeError on None -- a confusing error instead of the real one.
        # The caller shows this text in its status line.
        raise ValueError("project has no source path, so it has no audit inbox")
    with project_mutation_lock(project):
        d.mkdir(parents=True, exist_ok=True)
        layers = list_inaudit_layers(project)
        start = (max((x.number for x in layers), default=0) + 1)
        nxt, target, fd = reserve_next_layer(d, start)
        try:
            os.close(fd)
        except OSError:
            pass
        res = target.resolve()
    # PRIMARY commit (the durable empty layer) is done. The tracker write is
    # SECONDARY: a failure here must not turn "created" into "create failed".
    track_warning = record_user_layer(project, nxt)
    set_inaudit_selected(project, nxt)
    if track_warning:
        replay_tracker_repairs(project)
    return res

def validate_inaudit_path(project: Project, path: Path) -> bool:
    try:
        d = inaudit_dir(project)
        if d is None:
            return False
        p = Path(path).resolve()
        d.resolve()
        p.relative_to(d.resolve())
        if not INAUDIT_RE.match(p.name):
            return False
        return p.is_file()
    except Exception:
        return False


def delete_inaudit_layer(project: Project, number: int) -> str:
    """Deletes one canonical numbered INAUDIT layer.

    Returns "" on success or a short human-readable reason on failure so the UI
    can surface exactly why a layer could not be removed (locked by another
    process, already gone, traversal, invalid number).

    Edge cases handled:
      - number < 1 / non-canonical name -> rejected (never deletes foreign files).
      - path outside the project audit dir -> rejected.
      - file does not exist -> "already gone" (idempotent, still success-ish).
      - file locked by another process (OSError) -> clear reason, nothing deleted.
      - deletion of the currently selected layer -> selection falls back to the
        lowest remaining layer (get_inaudit_selected recomputes on next read).
    """
    d = inaudit_dir(project)
    if d is None:
        return "project has no source path"
    try:
        n = int(number)
        if n < 1:
            return "invalid layer number"
    except Exception:
        return "invalid layer number"
    cand = (d / f"{n}.md").resolve()
    try:
        cand.relative_to(d.resolve())
    except Exception:
        return "path is outside the audit directory"
    if not INAUDIT_RE.match(cand.name):
        return "not a canonical numbered layer"
    track_warning = ""
    with project_mutation_lock(project):
        if not cand.is_file():
            # Idempotent: nothing to remove. Also drop a stale selection entry so
            # the UI never points at a ghost layer.
            if _raw_inaudit_selected(project) == n:
                set_inaudit_selected(project, None)
            return ""
        try:
            cand.unlink()
        except PermissionError:
            return f"file is locked by another process: {cand.name}"
        except OSError as exc:
            return f"cannot delete {cand.name}: {exc}"
    if _raw_inaudit_selected(project) == n:
        set_inaudit_selected(project, None)
    # PRIMARY deletion committed above; tracker forget is SECONDARY.
    track_warning = forget_user_layer(project, n)
    if track_warning:
        replay_tracker_repairs(project)
    return ""


def rename_inaudit_layer(project: Project, number: int, new_number: int) -> str:
    """Move one canonical layer onto a DIFFERENT free number.

    The canonical name IS the number -- SAIPEN's audit inbox reads exactly
    `^[1-9][0-9]*\\.md$` -- so "rename" in this namespace means renumber, and the
    one thing it must never do is land on a layer somebody is working. Creation
    is exclusive: a taken number is refused rather than overwritten, which is the
    same rule the Bridge's own layer publication uses.

    Returns "" on success, or a short human-readable reason.
    """
    if project.source_path and is_managed(project.source_path):
        return "SAIPEN owns layer numbers; rename the capture title in Inbox instead"
    d = inaudit_dir(project)
    if d is None:
        return "project has no source path"
    try:
        old = int(number)
        new = int(new_number)
    except (TypeError, ValueError):
        return "invalid layer number"
    if old < 1 or new < 1:
        return "invalid layer number"
    if old == new:
        return ""
    source = (d / f"{old}.md").resolve()
    target = (d / f"{new}.md").resolve()
    try:
        root = d.resolve()
        source.relative_to(root)
        target.relative_to(root)
    except Exception:
        return "path is outside the audit directory"
    if not INAUDIT_RE.match(source.name) or not INAUDIT_RE.match(target.name):
        return "not a canonical numbered layer"
    if not source.is_file():
        return f"layer {old} does not exist"
    track_warning = ""
    with project_mutation_lock(project):
        if target.exists():
            return f"layer {new} already exists; pick a free number"
        try:
            # O_EXCL, then copy-and-remove: os.replace would silently overwrite a
            # layer created between the check above and the move.
            fd = open_exclusive_layer(target)
        except FileExistsError:
            return f"layer {new} was just taken; pick a free number"
        except OSError as exc:
            return f"cannot create {target.name}: {exc}"
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(source.read_bytes())
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
            return f"cannot write {target.name}: {exc}"
        try:
            source.unlink()
        except OSError as exc:
            # The new layer is durable; the old one is still there. Reported rather
            # than silently leaving two copies the operator cannot see.
            return f"copied to {target.name} but could not remove {source.name}: {exc}"
    if _raw_inaudit_selected(project) == old:
        set_inaudit_selected(project, new)
    # PRIMARY rename committed; tracker renumber is SECONDARY.
    track_warning = renumber_user_layer(project, old, new)
    if track_warning:
        replay_tracker_repairs(project)
    return ""


# ------------------------------------------------------------- reorder (CORE-003)

_SCRATCH_PREFIX = ".reorder-"
_SCRATCH_SUFFIX = ".tmp"

# Failure-injection hooks (CORE-003 tests): each receives ("park"|"place", src, dst)
# immediately before the physical move and may raise to abort at that boundary.
reorder_move_hooks: list = []


def _fire_move_hook(stage: str, src: Path, dst: Path) -> None:
    for hook in list(reorder_move_hooks):
        hook(stage, src, dst)


# Failure-injection hooks for RECOVERY (CORE-003 C4 tests): each receives
# ("stage"|"place", target_number) immediately before the recovery write and may
# raise to abort a second interruption mid-recovery.
reorder_recovery_hooks: list = []


def _fire_recovery_hook(stage: str, target_number: int) -> None:
    for hook in list(reorder_recovery_hooks):
        hook(stage, target_number)


def _layer_digest(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return "<unreadable>"


def _scratch_name(operation_id: str, source_number: int) -> str:
    """Hidden, operation-scoped, noncanonical scratch name.

    INAUDIT_RE never matches it (dot-prefixed, no N.md form), so agents and
    the inbox can never treat a parked layer as a numbered canonical one.
    """
    safe_op = re.sub(r"[^A-Za-z0-9_-]", "-", operation_id)[:32]
    return f"{_SCRATCH_PREFIX}{safe_op}-{source_number}{_SCRATCH_SUFFIX}"


def _recovery_scratch_name(operation_id: str, target_number: int) -> str:
    """Deterministic staging name for convergent CORE-003 recovery.

    Noncanonical (never matches INAUDIT_RE). A retry of an interrupted recovery
    finds the exact same staged body instead of having destroyed its only copy.
    """
    safe_op = re.sub(r"[^A-Za-z0-9_-]", "-", operation_id)[:32]
    return f"{_SCRATCH_PREFIX}{safe_op}-recover-{target_number}{_SCRATCH_SUFFIX}"


def _pre_state_fingerprint(project: Project) -> dict:
    layers = list_inaudit_layers(project)
    return {
        "layers": {str(layer.number): _layer_digest(layer.path) for layer in layers},
        "selection": _raw_inaudit_selected(project),
        "user_layers": sorted(_load_user_layers(user_layer_registry_path(project))),
    }


@dataclass
class _ReorderJournal:
    operation_id: str
    pre: dict
    final_order: list[int]
    moves: list[dict]  # [{src, scratch, dst}]
    phase: str  # "parking" | "committed" | "recovery_required"
    final_layers: dict = field(default_factory=dict)


def _reorder_journal_path(project: Project, operation_id: str) -> Path:
    safe_op = re.sub(r"[^A-Za-z0-9_-]", "-", operation_id)[:32]
    return get_user_runtime_dir() / _REORDER_DIR / f"{_project_digest(project)}-{safe_op}.json"


def _write_reorder_journal(project: Project, entry: _ReorderJournal) -> None:
    path = _reorder_journal_path(project, entry.operation_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "operation_id": entry.operation_id,
        "project_id": str(project.id),
        "pre": entry.pre,
        "final_order": entry.final_order,
        "final_layers": entry.final_layers,
        "moves": entry.moves,
        "phase": entry.phase,
    }
    _atomic_write_json(path, payload)


def _read_reorder_journal(project: Project, operation_id: str) -> Optional[dict]:
    try:
        return json.loads(_reorder_journal_path(project, operation_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _clear_reorder_journal(project: Project, operation_id: str) -> None:
    try:
        _reorder_journal_path(project, operation_id).unlink(missing_ok=True)
    except OSError:
        pass


def _pending_reorder_operations(project: Project) -> list[str]:
    """Every journaled reorder operation id for this project (bounded recovery)."""
    root = get_user_runtime_dir() / _REORDER_DIR
    if not root.is_dir():
        return []
    prefix = f"{_project_digest(project)}-"
    out = []
    for entry in root.glob(f"{prefix}*.json"):
        out.append(entry.name[len(prefix): -len(".json")])
    return out


def _fsync_directory(directory: Path) -> None:
    """Best-effort directory fsync (CORE-003 C5). No-op where unsupported."""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _recover_reorder_operation(project: Project, operation_id: str) -> str:
    """Resolve one interrupted reorder to exact pre-state or exact final state.

    CORE-003: every canonical entry is classified by EXPECTED IDENTITY (pre
    digest, planned final digest, or transaction-owned scratch), never by the
    integer filename alone. Any foreign same-number byte mutation makes recovery
    fail closed with nothing written and all evidence preserved. Recovery stages
    every body durably before the first canonical write, so a second interruption
    is itself recoverable and the next invocation converges.
    """
    journal = _read_reorder_journal(project, operation_id)
    if journal is None:
        return ""
    phase = journal.get("phase", "")
    if phase in ("committed", "committed_done"):
        # Terminal record: keep it -- it is the replay-idempotency evidence.
        return ""
    pre = journal.get("pre", {})
    pre_layers = pre.get("layers") or {}
    final_order = journal.get("final_order", [])
    moves = journal.get("moves", [])
    d = inaudit_dir(project)
    if d is None:
        return ""

    final_targets = list(range(1, len(final_order) + 1))
    expected_final = {str(at + 1): pre_layers.get(str(n)) for at, n in enumerate(final_order)}

    def _clear_scratch():
        for move in moves:
            try:
                (d / move.get("scratch", "")).unlink(missing_ok=True)
            except OSError:
                pass
        for target_number in final_targets:
            try:
                (d / _recovery_scratch_name(operation_id, target_number)).unlink(missing_ok=True)
            except OSError:
                pass

    current_layers = {
        str(layer.number): _layer_digest(layer.path) for layer in list_inaudit_layers(project)
    }
    pre_numbers = sorted(int(n) for n in pre_layers.keys())

    if set(int(n) for n in current_layers) - set(pre_numbers):
        # A foreign producer added layers the transaction never saw.
        return f"reorder recovery incomplete: foreign layer change, journal {operation_id} preserved"

    # C1: expected identity per canonical number = pre digest, plus the planned
    # placed body for every PLACE target.
    allowed: dict[int, set] = {}
    for n_str, digest in pre_layers.items():
        allowed.setdefault(int(n_str), set()).add(digest)
    for move in moves:
        src_digest = pre_layers.get(str(move.get("src")))
        if src_digest:
            allowed.setdefault(int(move.get("dst")), set()).add(src_digest)
    for n_str, digest in current_layers.items():
        if digest not in allowed.get(int(n_str), set()):
            return (
                f"reorder recovery incomplete: foreign layer bytes at {n_str}.md, "
                f"journal {operation_id} preserved"
            )
    for move in moves:
        scratch = d / move.get("scratch", "")
        if scratch.is_file() and _layer_digest(scratch) != pre_layers.get(str(move.get("src"))):
            return f"reorder recovery incomplete: unexpected scratch bytes, journal {operation_id} preserved"
    for target_number in final_targets:
        stage = d / _recovery_scratch_name(operation_id, target_number)
        if stage.is_file() and _layer_digest(stage) != expected_final.get(str(target_number)):
            return (
                f"reorder recovery incomplete: unexpected recovery staging, "
                f"journal {operation_id} preserved"
            )

    def _reconcile_final_metadata() -> None:
        """Map selection/tracker through the final permutation.

        Derived from the journaled PRE state, so replaying it is idempotent even
        if a previous recovery already reconciled. Only called once canonical
        recovery has committed the exact final bytes (CORE-003 test 7).
        """
        mapping = {int(move["src"]): int(move["dst"]) for move in moves}
        pre_sel = pre.get("selection")
        set_inaudit_selected(
            project, mapping.get(pre_sel, pre_sel) if pre_sel is not None else None
        )
        tracked = [int(n) for n in (pre.get("user_layers") or [])]
        updated = [mapping.get(n, n) for n in tracked]
        seen: list[int] = []
        deduped: list[int] = []
        for n in updated:
            if n not in seen:
                seen.append(n)
                deduped.append(n)
        try:
            with cross_process_lock(get_user_runtime_dir() / _USER_LAYER_LOCK):
                _save_user_layers(user_layer_registry_path(project), deduped)
        except Exception:  # noqa: BLE001 -- tracker is SECONDARY (CORE-002)
            _append_repair_intent(
                project,
                {"op": "renumber_map", "mapping": {str(k): v for k, v in mapping.items()}},
            )

    if current_layers == pre_layers:
        # Canonical set unchanged since planning: transaction never advanced
        # (or only left scratch residue). Restore exact pre-state.
        _clear_scratch()
        _clear_reorder_journal(project, operation_id)
        return ""
    if current_layers == expected_final:
        # Already at the exact committed final layout (a retry after a
        # partially-applied recovery): converge and clear.
        _reconcile_final_metadata()
        _clear_scratch()
        _clear_reorder_journal(project, operation_id)
        return ""

    def _body_for(digest: str) -> Optional[Path]:
        # Recovery staging first (survives a second interruption), then the
        # transaction scratch, then still-intact canonical sources.
        for target_number in final_targets:
            stage = d / _recovery_scratch_name(operation_id, target_number)
            if stage.is_file() and _layer_digest(stage) == digest:
                return stage
        for move in moves:
            scratch = d / move.get("scratch", "")
            if scratch.is_file() and _layer_digest(scratch) == digest:
                return scratch
        for number in pre_numbers:
            candidate = d / f"{number}.md"
            if candidate.is_file() and _layer_digest(candidate) == digest:
                return candidate
        return None

    # C4 preflight: locate every expected body BEFORE the first write.
    final_bodies: dict[int, bytes] = {}
    for target_number in final_targets:
        expected = expected_final.get(str(target_number))
        target = d / f"{target_number}.md"
        if target.is_file() and _layer_digest(target) == expected:
            continue  # already placed by a previous recovery attempt
        body = _body_for(expected) if expected else None
        if body is None:
            return f"reorder recovery incomplete: missing evidence, journal {operation_id} preserved"
        try:
            final_bodies[target_number] = body.read_bytes()
        except OSError:
            return f"reorder recovery incomplete: unreadable evidence, journal {operation_id} preserved"

    # Stage every body durably, then place. A second interruption therefore
    # always leaves the exact body at a deterministic path for the next retry.
    staged: list[tuple[Path, int]] = []
    for target_number, data in final_bodies.items():
        try:
            _fire_recovery_hook("stage", target_number)
            stage = d / _recovery_scratch_name(operation_id, target_number)
            fd, tmp = tempfile.mkstemp(prefix=".recovery-", suffix=".tmp", dir=str(d))
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp, stage)
            except OSError:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            staged.append((stage, target_number))
        except OSError:
            return f"reorder recovery failed: journal {operation_id} preserved"
    _fsync_directory(d)
    try:
        for stage, target_number in staged:
            _fire_recovery_hook("place", target_number)
            os.replace(stage, d / f"{target_number}.md")
        _fsync_directory(d)
    except OSError:
        return f"reorder recovery failed: journal {operation_id} preserved"
    # Verify the recovered state is exactly the committed final layout.
    recovered = {str(layer.number): _layer_digest(layer.path) for layer in list_inaudit_layers(project)}
    if recovered != expected_final:
        return f"reorder recovery verification failed: journal {operation_id} preserved"
    _reconcile_final_metadata()
    _clear_scratch()
    _clear_reorder_journal(project, operation_id)
    return ""


def recover_pending_reorders(project: Project) -> str:
    """Bounded startup/next-mutation recovery of every pending reorder."""
    reason = ""
    for operation_id in _pending_reorder_operations(project):
        result = _recover_reorder_operation(project, operation_id)
        if result and not reason:
            reason = result
    return reason


def reorder_inaudit_layers(project: Project, order: list[int], operation_id: Optional[str] = None) -> str:
    """Renumber layers so their canonical order equals ``order``.

    Drag-and-drop priority in the Layers list: the operator pulls a layer to
    the position where the agent should consume it, and the numbers follow.
    ``order`` is the NEW redisplay order as CURRENT layer numbers, e.g.
    ``[3, 1, 2]`` means "layer 3 first, layer 1 second, layer 2 third".

    CORE-003: one recoverable transaction under the project mutation lock.
    Scratch layers use hidden noncanonical names, a durable journal records
    the plan before the first physical move, and any failure rolls back to
    the exact pre-state (or leaves a RECOVERY journal when rollback itself
    fails). ``operation_id`` gives replay idempotency: the same operation id
    against the already-committed final fingerprint returns success without
    moving bytes; a new gesture gets a new id.
    """
    if project.source_path and is_managed(project.source_path):
        return "SAIPEN owns layer numbers; priority reorder is unavailable on managed projects"
    d = inaudit_dir(project)
    if d is None:
        return "project has no source path"
    if operation_id is None:
        operation_id = str(uuid.uuid4())

    with project_mutation_lock(project):
        # Bounded recovery boundary: resolve any interrupted reorder BEFORE
        # another mutation is accepted.
        recovery_reason = recover_pending_reorders(project)
        if recovery_reason:
            return recovery_reason

        existing = sorted(layer.number for layer in list_inaudit_layers(project))
        order = [int(value) for value in order]
        if not existing:
            return "no layers to reorder"
        if len(order) != len(existing) or sorted(order) != existing:
            return "layer list is stale; refresh and try again"
        if order == existing:
            _clear_reorder_journal(project, operation_id)
            return ""

        pre = _pre_state_fingerprint(project)
        pre_layers = pre["layers"]
        digest_by_pre_number = {int(n): digest for n, digest in pre_layers.items()}
        final_layers = {str(at + 1): digest_by_pre_number[number] for at, number in enumerate(order)}

        # Operation-id idempotency (CORE-003 D7):
        #   - Same id + current canonical state already equals the committed
        #     final fingerprint -> success, no second permutation.
        #   - Same id + expected pre-state differs (different layers set) ->
        #     stale replay -> refuse without moving bytes.
        previous = _read_reorder_journal(project, operation_id)
        if previous and previous.get("phase") in ("committed", "committed_done"):
            current_layers = {str(layer.number): _layer_digest(layer.path) for layer in list_inaudit_layers(project)}
            if current_layers == previous.get("final_layers"):
                _clear_reorder_journal(project, operation_id)
                return ""
            return f"stale reorder operation: {operation_id}"
        if previous:
            # A non-terminal journal from before (crash in progress) must be
            # resolved by recovery, not treated as a replay.
            _clear_reorder_journal(project, operation_id)

        moves: list[dict] = []
        for at, number in enumerate(order):
            target = at + 1
            if number != target:
                moves.append(
                    {
                        "src": number,
                        "scratch": _scratch_name(operation_id, number),
                        "dst": target,
                    }
                )

        journal = _ReorderJournal(
            operation_id=operation_id,
            pre=pre,
            final_order=order,
            moves=moves,
            phase="parking",
            final_layers=final_layers,
        )
        try:
            _write_reorder_journal(project, journal)
        except OSError as exc:
            return f"reorder failed writing journal: {exc}"

        def _fingerprint_layers() -> dict:
            return {str(layer.number): _layer_digest(layer.path) for layer in list_inaudit_layers(project)}

        def _rollback(reason: str, placed: list[dict], parked: list[dict]) -> str:
            # Roll back every completed park/place step to the exact pre-state.
            try:
                rollback_ok = True
                # 1. Undo completed PLACE steps (target file back to scratch).
                for move in reversed(placed):
                    target = d / f"{move['dst']}.md"
                    scratch = d / move["scratch"]
                    if target.exists():
                        if scratch.exists():
                            rollback_ok = False
                            break
                        os.replace(target, scratch)
                # 2. Undo completed PARK steps (scratch back to source).
                for move in reversed(parked):
                    scratch = d / move["scratch"]
                    source = d / f"{move['src']}.md"
                    if scratch.is_file():
                        if source.exists():
                            rollback_ok = False
                            break
                        os.replace(scratch, source)
                if rollback_ok:
                    # Verify canonical bytes match the pre-state digests.
                    current = {str(layer.number): _layer_digest(layer.path) for layer in list_inaudit_layers(project)}
                    if current != pre_layers:
                        rollback_ok = False
                if rollback_ok:
                    # Selection and tracker restore to the pre-state.
                    set_inaudit_selected(project, pre.get("selection"))
                    user_numbers = pre.get("user_layers") or []
                    # Rebuild tracker entries exactly as pre-state records them.
                    with cross_process_lock(get_user_runtime_dir() / _USER_LAYER_LOCK):
                        _save_user_layers(user_layer_registry_path(project), [int(v) for v in user_numbers])
                    _clear_reorder_journal(project, operation_id)
                    return reason
                # Rollback could not complete: durable RECOVERY_REQUIRED journal.
                journal.phase = "recovery_required"
                _write_reorder_journal(project, journal)
                return f"reorder failed: {reason}; recovery required"
            except OSError:
                journal.phase = "recovery_required"
                try:
                    _write_reorder_journal(project, journal)
                except OSError:
                    pass
                return f"reorder failed: {reason}; recovery required"

        parked: list[dict] = []
        placed: list[dict] = []
        try:
            # PARK phase: hide every moved layer under its noncanonical name.
            for move in moves:
                source = d / f"{move['src']}.md"
                scratch = d / move["scratch"]
                if scratch.exists():
                    return _rollback("scratch path already exists", placed, parked)
                if not source.is_file():
                    return _rollback(f"layer {move['src']} vanished mid-reorder", placed, parked)
                _fire_move_hook("park", source, scratch)
                os.replace(source, scratch)
                parked.append(move)

            # PLACE phase: park each scratch body at its final number.
            for move in parked:
                scratch = d / move["scratch"]
                target = d / f"{move['dst']}.md"
                if target.exists():
                    return _rollback(f"layer {move['dst']} was just taken", placed, parked)
                _fire_move_hook("place", scratch, target)
                os.replace(scratch, target)
                placed.append(move)

            # Verify the exact final state before declaring success.
            current = _fingerprint_layers()
            if current != final_layers:
                return _rollback("final state mismatch", placed, parked)

            # Tracker/selection update reflects the FINAL committed mapping.
            mapping = {move["src"]: move["dst"] for move in moves}
            selection_followed = _raw_inaudit_selected(project)
            if selection_followed is not None and selection_followed in mapping:
                set_inaudit_selected(project, mapping[selection_followed])
            try:
                with cross_process_lock(get_user_runtime_dir() / _USER_LAYER_LOCK):
                    tracked = _load_user_layers(user_layer_registry_path(project))
                    updated = [mapping.get(n, n) for n in tracked]
                    # De-duplicate: renumber must never duplicate ownership markers.
                    seen: list[int] = []
                    deduped: list[int] = []
                    for n in updated:
                        if n not in seen:
                            seen.append(n)
                            deduped.append(n)
                    _save_user_layers(user_layer_registry_path(project), deduped)
            except Exception:  # noqa: BLE001 -- tracker is SECONDARY (CORE-002)
                # The canonical reorder is committed; a tracker persistence
                # failure is a durable repair intent, never a reorder failure.
                _append_repair_intent(
                    project,
                    {
                        "op": "renumber_map",
                        "mapping": {str(k): v for k, v in mapping.items()},
                    },
                )

            # Keep the terminal journal on disk (phase "committed_done"): it
            # is the replay-idempotency evidence for a lost-response retry
            # with the same operation id.
            journal.phase = "committed_done"
            _write_reorder_journal(project, journal)
            return ""
        except OSError as exc:
            return _rollback(str(exc), placed, parked)
        except Exception as exc:  # noqa: BLE001
            return _rollback(f"reorder failed unexpectedly: {exc}", placed, parked)
