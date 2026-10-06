"""Which exact AUDAPACK implementation produced an archive (T-274).

Every archive must answer one question without opening a debugger: which
packing code wrote these bytes? Two facts are needed and they are different:

* the PRODUCER identity of the running process (version, source revision,
  entrypoint, mode, process start, the policy fingerprint), recorded in the
  manifest so two ZIPs can be compared; and
* whether the code this process LOADED still matches the code ON DISK -- a
  resident GUI/bridge keeps importing pre-fix modules long after the checkout
  changed, and that is exactly how a fixed inventory shipped a broken archive.

The digest is taken over the packing subsystem alone, so it moves when packing
semantics move and stays put when unrelated code changes. It is computed once
at import (that is what the process loaded) and re-computed on demand against
the files on disk. Nothing machine-private is recorded: no absolute paths, no
user names, only a content digest and the repository's own HEAD/dirty state.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

#: Files whose content decides what bytes an archive carries. A change in any
#: of them makes a resident process's already-imported copy obsolete.
PACKING_SUBSYSTEM_MODULES: tuple[str, ...] = (
    "packing.py",
    "source_inventory.py",
    "fidelity.py",
    "config.py",
    "services/packing_service.py",
)

CODE_STALE_PACKER_RUNTIME = "STALE_PACKER_RUNTIME"

_INVENTORY_SCHEMA_VERSION = 1  # audapack.packing.INVENTORY_MANIFEST_SCHEMA_VERSION


def _package_root() -> Path:
    # ``absolute()``, not ``resolve()``: this question is asked on every pack,
    # and ``resolve()`` stats the module file to canonicalize it (PERF-001 --
    # the pack must not re-read file metadata the traversal already saw). An
    # imported module's __file__ is already absolute, so there is nothing to
    # canonicalize for the purpose of naming a sibling file.
    return Path(__file__).absolute().parent


def packing_implementation_digest(root: Optional[Path] = None) -> str:
    """SHA-256 over the packing subsystem's own bytes, keyed by relative name.

    A missing file contributes a fixed marker instead of an exception: an
    unreadable module is a difference worth reporting, not a reason for the
    digest to blow up inside a pack.
    """
    base = Path(root) if root is not None else _package_root()
    hasher = hashlib.sha256()
    for rel in PACKING_SUBSYSTEM_MODULES:
        hasher.update(rel.encode("utf-8"))
        hasher.update(b"\x00")
        try:
            data = (base / rel).read_bytes()
        except OSError:
            hasher.update(b"\x00missing")
            continue
        hasher.update(hashlib.sha256(data).digest())
    return hasher.hexdigest()


#: The implementation THIS process imported. Captured at import time, which for
#: a resident GUI/bridge is process start -- restarting is what refreshes it.
LOADED_PACKING_DIGEST = packing_implementation_digest()


def _module_signature() -> tuple[tuple[str, int, int], ...]:
    """(relative name, size, mtime_ns) per covered module, from a directory read.

    ``os.scandir`` already carries size and mtime, so answering "nothing moved"
    costs one directory read and no per-file ``stat`` -- the same bargain the
    source walk makes (PERF-001), which matters because this question is asked
    on every pack. A module that vanished or cannot be read reports ``(-1, -1)``
    and therefore differs from the import signature: stale, never invisible.
    """
    root = _package_root()
    by_parent: dict[str, set[str]] = {}
    for rel in PACKING_SUBSYSTEM_MODULES:
        parent, _, name = rel.rpartition("/")
        by_parent.setdefault(parent, set()).add(name)
    rows: list[tuple[str, int, int]] = []
    for parent in sorted(by_parent):
        wanted = by_parent[parent]
        found: dict[str, tuple[int, int]] = {}
        try:
            with os.scandir(root / parent) as entries:
                for entry in entries:
                    if entry.name in wanted:
                        info = entry.stat()
                        found[entry.name] = (info.st_size, info.st_mtime_ns)
        except OSError:
            pass
        for name in sorted(wanted):
            size, mtime = found.get(name, (-1, -1))
            rows.append((f"{parent}/{name}" if parent else name, size, mtime))
    return tuple(rows)


#: Cheap signature of the covered modules as THIS process imported them.
_LOADED_MODULE_SIGNATURE = _module_signature()

#: Approximate process start: this module is imported while the runtime boots.
RUNTIME_STARTED_UTC = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def stale_packing_subsystem() -> Optional[str]:
    """Disk digest when it differs from the loaded one, else ``None``.

    Two steps, so the common answer stays free: the import-time signature is
    compared first and a match ends the question. Only a signature that moved
    pays for the content digest -- and a signature that moved while the bytes
    stayed identical (a touch, a checkout with restored mtimes) is not
    staleness, because what decides an archive's bytes is the content.
    """
    if _module_signature() == _LOADED_MODULE_SIGNATURE:
        return None
    current = packing_implementation_digest()
    return None if current == LOADED_PACKING_DIGEST else current


def runtime_mode(default: str = "gui") -> str:
    """How this process was started, when it can be told from the command line."""
    explicit = os.environ.get("AUDAPACK_RUNTIME_MODE", "").strip()
    if explicit:
        return explicit
    argv = [str(a) for a in sys.argv]
    if "--bridge" in argv:
        return "bridge"
    entry = Path(argv[0]).name.lower() if argv and argv[0] else ""
    if entry.endswith(".pyw"):
        return "gui"
    if entry:
        return "cli"
    return default


def _audapack_version() -> str:
    try:
        from audapack import __version__ as version

        return str(version)
    except Exception:  # pragma: no cover - version metadata must never break a pack
        return ""


def source_revision() -> dict:
    """Git identity of the AUDAPACK checkout, when it is one.

    Best effort by construction: a shipped copy has no repository, and the
    manifest must still be written. ``kind`` names which answer this is.
    """
    root = _package_root().parent
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if head.returncode != 0:
            raise OSError("not a git checkout")
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=20,
        )
        dirty = bool(status.stdout.strip()) if status.returncode == 0 else None
        return {"kind": "git", "head": head.stdout.strip(), "dirty": dirty}
    except Exception:
        return {
            "kind": "fingerprint",
            "packing_implementation_sha256": LOADED_PACKING_DIGEST,
        }


def producer_identity(
    *,
    runtime_mode_name: Optional[str] = None,
    policy_fingerprint: str = "",
    pack_started_utc: Optional[str] = None,
) -> dict:
    """The producer block written into an archive's manifests."""
    entry = ""
    argv = [str(a) for a in sys.argv]
    if argv and argv[0]:
        entry = Path(argv[0]).name
    return {
        "audapack_version": _audapack_version(),
        "source_revision": source_revision(),
        "packing_implementation_sha256": LOADED_PACKING_DIGEST,
        "process_start_utc": RUNTIME_STARTED_UTC,
        "pack_started_utc": pack_started_utc
        or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "runtime_entrypoint": entry,
        "runtime_mode": runtime_mode_name or runtime_mode(),
        "packing_policy_fingerprint": policy_fingerprint,
        "inventory_schema_version": _INVENTORY_SCHEMA_VERSION,
    }
