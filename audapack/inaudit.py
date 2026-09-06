from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from audapack.config import cross_process_lock, get_user_runtime_dir
from audapack.models import Project
from audapack.saipen_transport import is_managed

INAUDIT_RE = re.compile(r"^[1-9][0-9]*\.md$")

#: Bookkeeping file per project for layers the OPERATOR created through the
#: desktop app (the [+] button / ensure_next_layer). Widget-delivered layers
#: come from inaudit_capture.assign or the Bridge and are deliberately NEVER
#: recorded here, so the row [edit-last-custom] button can never land on an
#: AUDAPACK-widget layer. Kept OUTSIDE the audit dir: a sidecar inside it would
#: be packed into archives and read by the Agent Inbox residue scan.
_USER_LAYER_LOCK = "inaudit_user_layers.lock"


def user_layer_registry_path(project: Project) -> Optional[Path]:
    if not project or not project.id:
        return None
    digest = hashlib.sha256(str(project.id).encode("utf-8")).hexdigest()[:24]
    return get_user_runtime_dir() / "inaudit_user_layers" / f"{digest}.json"


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
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"user_layers": numbers, "schema_version": 1}).encode("utf-8")
    fd, temp_name = tempfile.mkstemp(prefix=".userlayers-", suffix=".tmp", dir=str(path.parent))
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


def _edit_user_layers(project: Project, mutate) -> None:
    path = user_layer_registry_path(project)
    lock_path = get_user_runtime_dir() / _USER_LAYER_LOCK
    with cross_process_lock(lock_path):
        numbers = _load_user_layers(path)
        mutate(numbers)
        _save_user_layers(path, numbers)


def record_user_layer(project: Project, number: int) -> None:
    """Mark a layer as operator-created. Absent here = opaque to [edit], so a
    widget-arrived layer is never edited by that button."""
    def _mutate(numbers: list[int]) -> None:
        if number in numbers:
            numbers.remove(number)
        numbers.append(number)

    _edit_user_layers(project, _mutate)


def forget_user_layer(project: Project, number: int) -> None:
    def _mutate(numbers: list[int]) -> None:
        try:
            numbers.remove(number)
        except ValueError:
            pass

    _edit_user_layers(project, _mutate)


def renumber_user_layer(project: Project, old: int, new: int) -> None:
    """The layer moved numbers (rename / reorder); the marker follows it.

    Only ``old`` is rewritten: a tracked entry for ``new`` either belongs to
    this same dance (the park+place reorder always rewrites it away first) or
    is a stale record for a file that is already gone, which the existence
    check in last_user_layer skips anyway.
    """
    def _mutate(numbers: list[int]) -> None:
        if old in numbers:
            numbers[:] = [new if value == old else value for value in numbers]

    _edit_user_layers(project, _mutate)


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
    d.mkdir(parents=True, exist_ok=True)
    layers = list_inaudit_layers(project)
    start = (max((x.number for x in layers), default=0) + 1)
    nxt, target, fd = reserve_next_layer(d, start)
    os.close(fd)
    res = target.resolve()
    record_user_layer(project, nxt)
    set_inaudit_selected(project, nxt)
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
    forget_user_layer(project, n)
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
    renumber_user_layer(project, old, new)
    return ""


def reorder_inaudit_layers(project: Project, order: list[int]) -> str:
    """Renumber layers so their canonical order equals ``order``.

    Drag-and-drop priority in the Layers list: the operator pulls a layer to
    the position where the agent should consume it, and the numbers follow.
    ``order`` is the NEW redisplay order as CURRENT layer numbers, e.g.
    ``[3, 1, 2]`` means "layer 3 first, layer 1 second, layer 2 third" -- the
    files are renumbered so ``1.md`` is the top layer, ``2.md`` next, and so
    on, in the requested sequence.

    Every step goes through `rename_inaudit_layer`, so a taken number is
    never overwritten: all moved layers are first parked on free scratch
    numbers, then placed at their final ``1..N`` targets. Selection and the
    user-layer tracker follow every step automatically.

    Returns "" on success, or a short human-readable reason (identical in
    spirit to the other primitives here, so the caller can surface it).
    """
    if project.source_path and is_managed(project.source_path):
        return "SAIPEN owns layer numbers; priority reorder is unavailable on managed projects"
    d = inaudit_dir(project)
    if d is None:
        return "project has no source path"
    existing = sorted(layer.number for layer in list_inaudit_layers(project))
    order = [int(value) for value in order]
    if not existing:
        return "no layers to reorder"
    if len(order) != len(existing) or sorted(order) != existing:
        return "layer list is stale; refresh and try again"
    if order == existing:
        return ""

    scratch = max(existing) + 1
    moves: list[tuple[int, int]] = []
    for at, number in enumerate(order):
        target = at + 1
        if number != target:
            moves.append((number, target))

    parked: list[int] = []
    try:
        for source, _target in moves:
            reason = rename_inaudit_layer(project, source, scratch)
            if reason:
                return f"reorder failed parking {source}: {reason}"
            parked.append(scratch)
            scratch += 1
        for index, (source, target) in enumerate(moves):
            reason = rename_inaudit_layer(project, parked[index], target)
            if reason:
                return f"reorder failed placing {source}: {reason}"
    except Exception:
        return "reorder failed unexpectedly"
    return ""
