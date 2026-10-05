"""Bridge run state management, concurrency locks, and cross-process generation signals."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import threading
import weakref
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from audapack.config import cross_process_lock, get_state_dir, open_new_temp_file

_GLOBAL_STATE_LOCK = threading.Lock()
# PERF-004: weak values so the in-process lock registry stays bounded by live
# contention instead of leaking one entry per run ever seen. A lock is kept
# alive while any caller holds a reference (run_transaction holds the returned
# lock across the critical section), so an entry is only evicted once no
# holder/waiters remain.
_RUN_LOCKS: "weakref.WeakValueDictionary[str, threading.Lock]" = weakref.WeakValueDictionary()


class RunStatePersistenceError(RuntimeError):
    """Raised when durable run-state replacement fails (CORE-013)."""


class RunStateCorruptionError(RuntimeError):
    """Raised when an existing durable run-state file is corrupt/unreadable (W2-004).

    Recovery must fail safely: an unreadable existing state is treated as
    evidence that a run exists with unknown committed content, never as proof
    that no run exists. Callers must not accept new wave writes against a
    freshly synthesized empty state in that case.
    """


class GenerationPersistenceError(RuntimeError):
    """Raised when the cross-process audit-generation signal cannot be published (W2-012)."""


class GenerationStateCorruptionError(GenerationPersistenceError):
    """Raised when an EXISTING durable generation file is corrupt/unreadable.

    CORE-001: a missing generation file is a legitimate fresh counter (bootstrap
    at 1), but an existing-but-unreadable file is evidence of unknown prior
    durable history. It must never be collapsed into generation 0, and the
    counter must never be republished lower than the last durable value. The
    corrupt bytes are left untouched so an explicit recovery can quarantine and
    restart the stream under a new epoch.
    """


def canonical_run_key(run_id: str) -> str:
    """Canonical persistent identity of a run: sha256 of the FULL raw run id.

    The state filename AND the in-process lock key derive from this same value,
    so two distinct runs can never share a state file even when their sanitized
    forms collide.
    """
    return hashlib.sha256(run_id.strip().encode("utf-8")).hexdigest()[:16]


def get_run_lock(run_id: str) -> threading.Lock:
    """Returns a dedicated lock for serializing writes belonging to the same run.

    Keyed by the canonical full-run identity -- the same key that names the
    state file -- never by the truncated sanitized form.
    """
    key = canonical_run_key(run_id)
    with _GLOBAL_STATE_LOCK:
        lock = _RUN_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _RUN_LOCKS[key] = lock
        return lock


def get_run_transaction_lock_path(run_id: str, base_dir: Optional[Path] = None) -> Path:
    """Deterministic cross-process lock file for one run's complete transaction.

    Lives beside the run state so all Bridge processes sharing a state dir
    coordinate on the same owner (W2-001).
    """
    return get_bridge_state_dir(base_dir) / "locks" / f"run_{canonical_run_key(run_id)}.lock"


@contextlib.contextmanager
def run_transaction(run_id: str, base_dir: Optional[Path] = None) -> Iterator[None]:
    """Serializes one campaign's complete durable transaction across threads AND
    processes.

    W2-001: the in-process threading.Lock alone cannot coordinate two Bridge
    daemons sharing the canonical runtime. This holds a cross-process file lock
    keyed by canonical_run_key(run_id) for the same region previously protected
    only by the in-process lock, then acquires the cheap thread lock inside so
    overlapping same-process requests stay serialized without extra OS handles.
    """
    lock_file = get_run_transaction_lock_path(run_id, base_dir)
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    thread_lock = get_run_lock(run_id)
    with cross_process_lock(lock_file):
        with thread_lock:
            yield


def get_bridge_state_dir(base_dir: Optional[Path] = None) -> Path:
    if base_dir:
        state_dir = base_dir / "runs"
    else:
        state_dir = get_state_dir() / "runs"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir


def sanitize_run_id(run_id: str) -> str:
    """Legacy filename form (kept only for reading pre-hash migration files)."""
    cleaned = re.sub(r"[^a-zA-Z0-9_-]", "_", run_id.strip())
    return cleaned[:64] or "run"


def get_run_state_file(run_id: str, base_dir: Optional[Path] = None) -> Path:
    return get_bridge_state_dir(base_dir) / f"run_{canonical_run_key(run_id)}.json"


def get_run_state(run_id: str, base_dir: Optional[Path] = None) -> dict[str, Any]:
    state_file = get_run_state_file(run_id, base_dir)
    if not state_file.exists():
        # Legacy migration: read a pre-hash sanitized file if present and copy
        # it forward under the hashed identity without deleting the original.
        legacy_file = get_bridge_state_dir(base_dir) / f"{sanitize_run_id(run_id)}.json"
        if legacy_file.exists():
            try:
                legacy_state = json.loads(legacy_file.read_text(encoding="utf-8"))
                save_run_state(run_id, legacy_state, base_dir)
                return legacy_state
            except Exception as exc:
                raise RunStateCorruptionError(
                    f"Legacy run state file {legacy_file} is unreadable/corrupt: {exc}"
                ) from exc
    if state_file.exists():
        try:
            return json.loads(state_file.read_text(encoding="utf-8"))
        except Exception as exc:
            raise RunStateCorruptionError(
                f"Run state file {state_file} is corrupt or unreadable: {exc}"
            ) from exc
    return {
        "schema_version": 2,
        "run_id": run_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "project_id": "",
        "project": "",
        "history_dir": "",
        "waves": {},
        "all3_complete": False,
    }


def save_run_state(run_id: str, state: dict[str, Any], base_dir: Optional[Path] = None):
    """Durably replaces the run state file.

    CORE-013: any write/replace failure raises RunStatePersistenceError instead of
    silently returning. Callers must not report a delivery as durably accepted
    unless this succeeds, so a failed state replacement must not be swallowed.
    """
    state_dir = get_bridge_state_dir(base_dir)
    state_file = get_run_state_file(run_id, base_dir)
    # Unpredictable name + O_EXCL: a predictable temp opened with a plain
    # open() is a write primitive for anyone who can plant a link here first.
    fd, tmp_file = open_new_temp_file(state_dir, state_file.name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        tmp_file.replace(state_file)
    except Exception as exc:
        if tmp_file.exists():
            try:
                tmp_file.unlink()
            except OSError:
                pass
        raise RunStatePersistenceError(f"Failed to durably persist run state for {run_id}: {exc}") from exc


def get_generation_file_path(base_dir: Optional[Path] = None) -> Path:
    if base_dir:
        return base_dir / "audit_generation.json"
    return get_state_dir() / "audit_generation.json"


def _new_generation_epoch() -> str:
    """Opaque recovery identity for one generation stream.

    W2-003: a generation counter and the epoch it belongs to are two facts.
    When durable bytes are unrecoverable the counter restarts, and the epoch
    change is what tells a consumer to resync instead of ignoring a lower
    number. The token is random so an unrelated corrupt file cannot resurrect
    the old stream's identity.
    """
    return os.urandom(16).hex()


def read_generation_document(base_dir: Optional[Path] = None) -> dict[str, Any]:
    """Read the audit generation document, distinguishing MISSING from CORRUPT.

    Returns a dict with an added ``exists`` flag. A missing file is a valid
    fresh counter (``exists`` False, generation 0, empty epoch). An existing
    file that cannot be parsed into a non-negative integer generation raises
    :class:`GenerationStateCorruptionError` -- it is NEVER reported as 0.
    """
    g_file = get_generation_file_path(base_dir)
    if not g_file.exists():
        return {
            "generation": 0,
            "epoch": "",
            "exists": False,
            "project_id": "",
            "last_project": "",
            "last_wave": "",
            "updated_at": "",
        }
    try:
        raw = json.loads(g_file.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise GenerationStateCorruptionError(
            f"audit generation file {g_file} is corrupt or unreadable: {exc}"
        ) from exc
    if not isinstance(raw, dict):
        raise GenerationStateCorruptionError(
            f"audit generation file {g_file} is not a JSON object"
        )
    generation = raw.get("generation")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
        raise GenerationStateCorruptionError(
            f"audit generation file {g_file} has no valid non-negative generation"
        )
    doc = dict(raw)
    doc["exists"] = True
    doc.setdefault("epoch", "")
    return doc


def get_audit_generation(base_dir: Optional[Path] = None) -> dict[str, Any]:
    """Reads current audit generation counter for cross-process GUI synchronization.

    CORE-001: raises :class:`GenerationStateCorruptionError` for an existing
    unreadable file rather than inferring generation 0.
    """
    return read_generation_document(base_dir)


get_generation_info = get_audit_generation


def _quarantine_generation_file(g_file: Path) -> Optional[Path]:
    """Move corrupt generation bytes aside instead of destroying evidence."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    target = g_file.parent / f"{g_file.name}.corrupt-{stamp}.json"
    try:
        g_file.replace(target)
        return target
    except OSError:
        return None


def _write_generation_document(g_file: Path, data: dict[str, Any]) -> None:
    fd, tmp_file = open_new_temp_file(g_file.parent, g_file.name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        tmp_file.replace(g_file)
    except Exception as exc:
        if tmp_file.exists():
            try:
                tmp_file.unlink()
            except OSError:
                pass
        raise GenerationPersistenceError(
            f"Failed to publish audit generation: {exc}"
        ) from exc


def recover_audit_generation(base_dir: Optional[Path] = None) -> int:
    """Explicitly recover a missing or corrupt audit generation stream.

    W2-003: quarantine unreadable bytes and start a NEW epoch at generation 1 so
    consumers resync and adopt the recovered baseline even though the numeric
    counter is lower than the last value they observed. A healthy stream is a
    no-op and returns its current generation.
    """
    g_file = get_generation_file_path(base_dir)
    lock_file = g_file.parent / "audit_generation.lock"
    with cross_process_lock(lock_file):
        try:
            current = read_generation_document(base_dir)
        except GenerationStateCorruptionError:
            _quarantine_generation_file(g_file)
            current = {"generation": 0, "epoch": "", "exists": False}
        if current.get("exists"):
            return int(current.get("generation", 0))
        _write_generation_document(g_file, {
            "generation": 1,
            "epoch": _new_generation_epoch(),
            "project_id": "",
            "last_project": "",
            "last_wave": "",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
        return 1


def publish_audit_generation(
    project_name: str,
    wave: str,
    base_dir: Optional[Path] = None,
    *,
    project_id: Optional[str] = None,
    recover_on_corruption: bool = True,
) -> int:
    """Post-commit generation publication that never suppresses authoritative commit.

    Contract:
    - The caller's authoritative data is ALREADY committed when this is invoked.
    - If the generation file is missing, publish 1 (bootstrap).
    - If the generation file is corrupt:
      * the normal call path self-heals via ``recover_audit_generation()`` before
        publishing, so the deadbeat loop cannot happen;
      * callers whose call ALREADY succeeded must stay successful even if this
        publish cannot be delivered -- the returned error is informational.
    - ``GenerationStateCorruptionError`` is a ``GenerationPersistenceError``
      so ``except GenerationPersistenceError`` boundaries already cover it.

    Returns the new generation. Raises ``GenerationPersistenceError`` if
    publication genuinely failed (including a recovery that failed).
    """
    try:
        return increment_audit_generation(project_name, wave, base_dir, project_id=project_id)
    except GenerationStateCorruptionError:
        if not recover_on_corruption:
            raise
        recover_audit_generation(base_dir)
        return increment_audit_generation(project_name, wave, base_dir, project_id=project_id)


def increment_audit_generation(
    project_name: str,
    wave: str,
    base_dir: Optional[Path] = None,
    project_id: Optional[str] = None,
) -> int:
    """
    Atomically increments the audit generation counter (cross-process safe).

    The whole read-modify-write runs under a cross-process lock: atomic replace
    alone prevents file corruption but NOT lost increments, since two writers
    could both read generation N and both publish N+1.
    """
    g_file = get_generation_file_path(base_dir)
    lock_file = g_file.parent / "audit_generation.lock"
    with cross_process_lock(lock_file):
        # CORE-001: read_generation_document raises for an existing corrupt
        # file, so no synthetic 0 can leak in and no lower generation is ever
        # published. The corrupt bytes stay on disk for explicit recovery.
        current = read_generation_document(base_dir)
        next_gen = int(current.get("generation", 0)) + 1
        epoch = str(current.get("epoch") or "") or _new_generation_epoch()
        new_data = {
            "generation": next_gen,
            "epoch": epoch,
            "project_id": project_id or "",
            "last_project": project_name,
            "last_wave": wave,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        _write_generation_document(g_file, new_data)
        return next_gen
