from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from audapack.models import Project

INAUDIT_RE = re.compile(r"^[1-9][0-9]*\.md$")

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

def ensure_next_layer(project: Project) -> Path:
    d = inaudit_dir(project)
    if d is None:
        # An assert here vanished under python -O and left d.mkdir raising
        # AttributeError on None -- a confusing error instead of the real one.
        # The caller shows this text in its status line.
        raise ValueError("project has no source path, so it has no audit inbox")
    d.mkdir(parents=True, exist_ok=True)
    layers = list_inaudit_layers(project)
    nxt = (max((x.number for x in layers), default=0) + 1)
    if nxt < 1:
        nxt = 1
    target = d / f"{nxt}.md"
    if not target.exists():
        target.write_text("", encoding="utf-8")
    res = target.resolve()
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
        if get_inaudit_selected(project) == n:
            set_inaudit_selected(project, None)
        return ""
    try:
        cand.unlink()
    except PermissionError:
        return f"file is locked by another process: {cand.name}"
    except OSError as exc:
        return f"cannot delete {cand.name}: {exc}"
    if get_inaudit_selected(project) == n:
        set_inaudit_selected(project, None)
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
        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0))
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
    if get_inaudit_selected(project) == old:
        set_inaudit_selected(project, new)
    return ""
