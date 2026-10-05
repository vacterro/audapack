"""Crash-safe commit for an EXISTING INAUDIT canonical layer (CORE-001).

The old Qt path mutated ``audit/N.md`` in place with ``open("r+b")`` ->
``seek/write/truncate``. That protects against stale editor content and a
consume that happens *before* open, but not durability: a process death or
I/O failure after mutation begins can leave the only canonical layer
partially written or truncated. A naive ``os.replace(temp, layer)`` fix is
also wrong -- it can recreate a layer SAIPEN consumed between revalidation
and replacement.

This module owns the durability logic so no QWidget does. It guarantees both
invariants:

1. After a failed/interrupted Save the canonical bytes are EXACTLY the old
   bytes or EXACTLY the new bytes -- never a prefix/truncated hybrid.
2. A concurrently consumed/deleted canonical layer is never recreated.

On Windows the commit uses ``ReplaceFileW``, which atomically replaces an
already-existing target and fails (instead of recreating) when the target
vanished. On other platforms a journal + durable backup + atomic rename
transaction with the same postcondition checks is used, and crash recovery
resolves any interrupted journal deterministically (always to exact OLD or
exact NEW).

Failure injection hooks (``commit_stage_hooks``) let tests reproduce every
durable-commit boundary without touching the production code path.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

#: Platform selector captured once. Tests override it to exercise the POSIX
#: commit path on a Windows host without mutating the global ``sys.platform``.
_PLATFORM = sys.platform

# Outcome constants returned by ``LayerCommitResult.outcome``.
COMMITTED = "COMMITTED"
MISSING_OR_CONSUMED = "MISSING_OR_CONSUMED"
CHANGED_EXTERNALLY = "CHANGED_EXTERNALLY"
RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
IO_FAILED = "IO_FAILED"

# Recovery outcomes (returned by ``recover_pending_layer_commit``).
RECOVERED_OLD = "RECOVERED_OLD"
RECOVERED_NEW = "RECOVERED_NEW"
ABSENT = "ABSENT"
CONFLICT = "CONFLICT"

_JOURNAL_PREFIX = ".save-"  # e.g. ".save-9.md.save-journal"
_JOURNAL_SUFFIX = ".save-journal"
_TEMP_SUFFIX = ".save-new"
_BACKUP_SUFFIX = ".save-old"

# Ordered stages a commit passes through. Tests register hooks keyed by name.
STAGE_JOURNAL_CREATE = "journal_create"
STAGE_JOURNAL_FSYNC = "journal_fsync"
STAGE_BACKUP_WRITE = "backup_write"
STAGE_TEMP_WRITE = "temp_write"
STAGE_TEMP_FSYNC = "temp_fsync"
STAGE_BEFORE_COMMIT = "before_commit"
STAGE_AFTER_COMMIT = "after_commit"
STAGE_DIR_FSYNC = "dir_fsync"
STAGE_CLEANUP = "cleanup"
STAGE_RECOVERY_START = "recovery_start"
STAGE_RECOVERY_APPLY = "recovery_apply"
STAGE_RECOVERY_CLEANUP = "recovery_cleanup"

commit_stage_hooks: list[Callable[[str], None]] = []


def _stage(name: str) -> None:
    """Fire registered failure-injection hooks for ``name``.

    A hook raising aborts the commit at exactly that durable boundary, which
    is how the red-first tests reproduce mid-transaction crashes.
    """
    for hook in list(commit_stage_hooks):
        hook(name)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _journal_path(target: Path) -> Path:
    return target.with_name(_JOURNAL_PREFIX + target.name + _JOURNAL_SUFFIX)


def _temp_path(target: Path) -> Path:
    return target.with_name(_JOURNAL_PREFIX + target.name + _TEMP_SUFFIX)


def _backup_path(target: Path) -> Path:
    return target.with_name(_JOURNAL_PREFIX + target.name + _BACKUP_SUFFIX)


def _fsync_file(path: Path) -> None:
    # Windows: FlushFileBuffers requires a write handle; r+b leaves bytes intact.
    with open(path, "r+b" if path.exists() else "ab") as handle:
        os.fsync(handle.fileno())


def _fsync_dir(directory: Path) -> None:
    if sys.platform == "win32":
        # Windows refuses os.open() on directories; flush via a real handle.
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.restype = ctypes.c_void_p
        handle = k32.CreateFileW(
            ctypes.c_wchar_p(str(directory)),
            0x80000000,  # GENERIC_READ
            7,  # FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE
            None,
            3,  # OPEN_EXISTING
            0x02000000,  # FILE_FLAG_BACKUP_SEMANTICS
            None,
        )
        if handle in (None, -1, 0xFFFFFFFFFFFFFFFF):
            return
        try:
            k32.FlushFileBuffers(handle)
        finally:
            k32.CloseHandle(handle)
        return
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


def _durable_write_bytes(path: Path, data: bytes) -> None:
    """Write ``data`` atomically into ``path`` with fsync of content + dir."""
    directory = path.parent
    fd, tmp = tempfile.mkstemp(prefix=".write-", suffix=".tmp", dir=str(directory))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _fsync_dir(directory)


def _remove_if_exists(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


class _DestinationMissing(Exception):
    """Raised when the commit target vanished before replacement."""


class _UnsupportedSafeReplace(Exception):
    """No atomic existing-target replacement primitive exists on this platform.

    CORE-001 A5: refusing is correct -- a generic ``os.replace`` can resurrect a
    canonical layer SAIPEN consumed between validation and the swap.
    """


# Linux/glibc ``renameat2`` flags (also the values used by the raw syscall).
_AT_FDCWD = -100
_RENAME_EXCHANGE = 0x2
# macOS ``renamex_np`` swap flag.
_RENAME_SWAP = 0x2

_rename_exchange_impl: Optional[Callable[[Path, Path], None]] = None
_rename_exchange_probed = False


def _load_rename_exchange() -> Optional[Callable[[Path, Path], None]]:
    """Return a native atomic EXCHANGE callable, or None when unsupported.

    Both files must already exist and the operation must fail (not recreate)
    when the target is gone. ``renameat2(RENAME_EXCHANGE)`` on Linux and
    ``renamex_np(RENAME_SWAP)`` on macOS satisfy that; Python's ``os``/``posix``
    module exposes neither on every build, so call the libc symbol directly.
    """
    global _rename_exchange_impl, _rename_exchange_probed
    if _rename_exchange_probed:
        return _rename_exchange_impl
    _rename_exchange_probed = True
    try:
        libc = ctypes.CDLL(None, use_errno=True)
    except OSError:
        return None

    if _PLATFORM.startswith("linux"):
        try:
            fn = libc.renameat2
        except AttributeError:
            return None
        fn.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        fn.restype = ctypes.c_int

        def _linux_exchange(replacement: Path, target: Path) -> None:
            rc = fn(
                _AT_FDCWD,
                os.fsencode(str(replacement)),
                _AT_FDCWD,
                os.fsencode(str(target)),
                _RENAME_EXCHANGE,
            )
            if rc != 0:
                err = ctypes.get_errno()
                if err == errno.ENOENT:
                    raise _DestinationMissing()
                raise OSError(err, os.strerror(err), str(target))

        _rename_exchange_impl = _linux_exchange
        return _rename_exchange_impl

    if _PLATFORM == "darwin":
        try:
            fn = libc.renamex_np
        except AttributeError:
            return None
        fn.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        fn.restype = ctypes.c_int

        def _darwin_exchange(replacement: Path, target: Path) -> None:
            rc = fn(os.fsencode(str(replacement)), os.fsencode(str(target)), _RENAME_SWAP)
            if rc != 0:
                err = ctypes.get_errno()
                if err == errno.ENOENT:
                    raise _DestinationMissing()
                raise OSError(err, os.strerror(err), str(target))

        _rename_exchange_impl = _darwin_exchange
        return _rename_exchange_impl

    return None


def _native_rename_exchange(replacement: Path, target: Path) -> None:
    """Atomically exchange two existing directory entries (test seam)."""
    impl = _load_rename_exchange()
    if impl is None:
        raise _UnsupportedSafeReplace()
    impl(Path(replacement), Path(target))


def _posix_replace_existing_only(replacement: Path, target: Path, backup: Optional[Path]) -> None:
    """Existing-only atomic replacement on POSIX via native EXCHANGE.

    After a successful exchange the old canonical bytes live at ``replacement``.
    When a ``backup`` path is requested they are moved there (the cross-platform
    contract: the backup holds the superseded content). Without a backup the
    caller owns the scratch path and cleans it.
    """
    _native_rename_exchange(replacement, target)
    if backup is not None:
        try:
            os.replace(str(replacement), str(backup))
        except OSError:
            pass


def _replace_file_existing_only(replacement: Path, target: Path, backup: Optional[Path]) -> None:
    """Replace ``target`` with ``replacement`` only if ``target`` still exists.

    Windows: ``ReplaceFileW`` atomically swaps the files and fails (instead of
    recreating) when the replaced file is gone. The backup file receives the
    superseded (old) content atomically.

    POSIX: a native atomic EXCHANGE (Linux ``renameat2`` / macOS ``renamex_np``)
    swaps both directory entries and fails with ENOENT when the target is gone.
    When no such primitive exists the operation refuses with
    ``_UnsupportedSafeReplace`` BEFORE any canonical mutation -- a generic
    ``os.replace`` is never used as an existing-only replacement because it can
    resurrect a consumed layer.
    """
    if _PLATFORM == "win32":
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        ok = k32.ReplaceFileW(
            ctypes.c_wchar_p(str(target)),
            ctypes.c_wchar_p(str(replacement)),
            ctypes.c_wchar_p(str(backup)) if backup is not None else None,
            0,
            None,
            None,
        )
        if not ok:
            err = ctypes.get_last_error()
            if err in (2, 3):  # ERROR_FILE_NOT_FOUND / ERROR_PATH_NOT_FOUND
                raise _DestinationMissing()
            raise OSError(err, os.strerror(err), str(target))
        return

    _posix_replace_existing_only(replacement, target, backup)


@dataclass(frozen=True)
class LayerCommitResult:
    outcome: str
    detail: str = ""
    committed_bytes: Optional[bytes] = None


def commit_existing_layer(
    path,
    expected_bytes: bytes,
    new_bytes: bytes,
) -> LayerCommitResult:
    """Durably commit ``new_bytes`` onto the existing layer ``path``.

    Preconditions: ``path`` must already exist and currently hold exactly
    ``expected_bytes``. Returns a structured result instead of raising
    ambiguous exceptions.
    """
    target = Path(path)
    directory = target.parent

    # Precondition 1: target must exist (never recreate a consumed layer).
    if not target.exists():
        return LayerCommitResult(MISSING_OR_CONSUMED, "layer is missing or already consumed")
    try:
        current = target.read_bytes()
    except OSError as exc:
        return LayerCommitResult(IO_FAILED, f"cannot read layer: {exc}")
    if current == new_bytes:
        # Idempotent replay after the previous commit already landed.
        return LayerCommitResult(COMMITTED, "layer already holds the new content", new_bytes)
    if current != expected_bytes:
        return LayerCommitResult(CHANGED_EXTERNALLY, "layer content changed since it was loaded")

    # Resolve any interrupted commit from a previous run before starting a new one.
    recovered = recover_pending_layer_commit(target)
    if recovered is not None and recovered.outcome in (RECOVERED_NEW,):
        # An earlier commit already produced the new content; report committed.
        if (directory / target.name).read_bytes() == new_bytes if target.exists() else False:
            return LayerCommitResult(COMMITTED, "prior commit recovered to new content", new_bytes)

    old_sha = _sha256(expected_bytes)
    new_sha = _sha256(new_bytes)
    journal = _journal_path(target)
    temp = _temp_path(target)
    backup = _backup_path(target)

    try:
        # Durable recovery journal BEFORE any destructive mutation.
        _stage(STAGE_JOURNAL_CREATE)
        _durable_write_bytes(
            journal,
            json.dumps(
                {
                    "schema_version": 1,
                    "target": target.name,
                    "old_sha256": old_sha,
                    "new_sha256": new_sha,
                },
                separators=(",", ":"),
            ).encode("utf-8"),
        )
        _stage(STAGE_JOURNAL_FSYNC)
        _fsync_file(journal)

        # New content, same dir (filesystem-local move keeps atomicity).
        _stage(STAGE_TEMP_WRITE)
        _remove_if_exists(temp)
        fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(new_bytes)
                handle.flush()
                _stage(STAGE_TEMP_FSYNC)
                os.fsync(handle.fileno())
        except BaseException:
            _remove_if_exists(temp)
            raise

        _stage(STAGE_BEFORE_COMMIT)
        # Final pre-commit precondition: refuse if the target changed under us
        # (a consumer or foreign writer), never overwriting foreign bytes.
        try:
            current_before_commit = target.read_bytes()
        except FileNotFoundError:
            _cleanup_scratch(journal, temp, backup)
            return LayerCommitResult(
                MISSING_OR_CONSUMED, "layer was consumed before commit; nothing recreated"
            )
        except OSError as exc:
            _cleanup_scratch(journal, temp, backup)
            return LayerCommitResult(IO_FAILED, f"cannot read layer before commit: {exc}")
        if current_before_commit != expected_bytes:
            _cleanup_scratch(journal, temp, backup)
            return LayerCommitResult(CHANGED_EXTERNALLY, "layer changed before final commit")
        try:
            _replace_file_existing_only(temp, target, backup)
        except _DestinationMissing:
            _cleanup_scratch(journal, temp, backup)
            return LayerCommitResult(
                MISSING_OR_CONSUMED, "layer was consumed before commit; nothing recreated"
            )
        except _UnsupportedSafeReplace:
            _cleanup_scratch(journal, temp, backup)
            return LayerCommitResult(
                IO_FAILED, "unsupported safe existing-layer replacement; nothing written"
            )
        _stage(STAGE_AFTER_COMMIT)
        try:
            if backup is not None and backup.exists():
                _fsync_file(backup)
        except OSError:
            pass
        _fsync_dir(directory)

        # Verify and remove the journal only once the commit is durable.
        try:
            _stage(STAGE_DIR_FSYNC)
            verified = target.read_bytes()
        except OSError:
            verified = b""

        if verified != new_bytes:
            # Commit physically happened but could not be proven durable: roll
            # back to the exact old bytes when the same target still holds ours.
            if target.exists() and target.read_bytes() == new_bytes and backup.exists():
                try:
                    _replace_file_existing_only(backup, target, None)
                    _fsync_dir(directory)
                except _DestinationMissing:
                    _stage(STAGE_CLEANUP)
                    _cleanup_scratch(journal, temp, backup)
                    return LayerCommitResult(
                        RECOVERY_REQUIRED, "commit unverifiable and target already consumed"
                    )
            _stage(STAGE_CLEANUP)
            _cleanup_scratch(journal, temp, backup)
            return LayerCommitResult(IO_FAILED, "commit could not be verified; rolled back to old bytes")

        # Cleanup hook fires once the commit is already durable; a hook error
        # here must NOT roll back committed bytes.
        try:
            _stage(STAGE_CLEANUP)
        except Exception:
            pass
        _cleanup_scratch(journal, temp, backup)
        return LayerCommitResult(COMMITTED, "layer committed", new_bytes)
    except _DestinationMissing:
        _cleanup_scratch(journal, temp, backup)
        return LayerCommitResult(MISSING_OR_CONSUMED, "layer was consumed during commit")
    except Exception as exc:  # noqa: BLE001 -- surface as structured failure
        # Attempt to restore exact old bytes if the commit partially applied.
        try:
            if target.exists() and target.read_bytes() == new_bytes and backup.exists():
                _replace_file_existing_only(backup, target, None)
                _fsync_dir(directory)
        except Exception:
            _stage(STAGE_CLEANUP)
            _cleanup_scratch(journal, temp, backup)
            return LayerCommitResult(
                RECOVERY_REQUIRED, f"commit failed mid-transaction: {exc}"
            )
        _cleanup_scratch(journal, temp, backup)
        return LayerCommitResult(IO_FAILED, f"commit failed: {exc}")


def _cleanup_scratch(journal: Path, temp: Path, backup: Path) -> None:
    _remove_if_exists(temp)
    _remove_if_exists(backup)
    _remove_if_exists(journal)


def recover_pending_layer_commit(target) -> Optional[LayerCommitResult]:
    """Resolve a stale ``commit_existing_layer`` journal for ``target``.

    Called at the start of a fresh commit and at a bounded project-layer
    boundary. Deterministic and idempotent: the canonical layer always ends up
    holding EXACT old or EXACT new bytes, and a foreign file is never touched.
    """
    target = Path(target)
    journal = _journal_path(target)
    if not journal.exists():
        return None

    _stage(STAGE_RECOVERY_START)
    temp = _temp_path(target)
    backup = _backup_path(target)
    try:
        data = json.loads(journal.read_text(encoding="utf-8"))
        old_sha = data.get("old_sha256")
        new_sha = data.get("new_sha256")
    except (OSError, ValueError):
        _cleanup_scratch(journal, temp, backup)
        return LayerCommitResult(IO_FAILED, "recovery journal unreadable")

    def _cleanup() -> None:
        _stage(STAGE_RECOVERY_CLEANUP)
        _cleanup_scratch(journal, temp, backup)

    if not target.exists():
        # Consumed before/during commit -- leave it absent, never recreate.
        _cleanup()
        return LayerCommitResult(ABSENT, "layer consumed; nothing recreated")

    _stage(STAGE_RECOVERY_APPLY)
    try:
        current_sha = _sha256(target.read_bytes())
    except OSError as exc:
        _cleanup()
        return LayerCommitResult(IO_FAILED, f"cannot read layer during recovery: {exc}")

    if current_sha == new_sha:
        _cleanup()
        return LayerCommitResult(RECOVERED_NEW, "layer already holds the committed content")
    if current_sha == old_sha:
        # Transaction never reached commit; resolve to exact old state.
        _cleanup()
        return LayerCommitResult(RECOVERED_OLD, "layer reverted to pre-commit content")
    # Foreign or partial bytes: leave them alone, only dispose our scratch.
    _remove_if_exists(temp)
    _remove_if_exists(backup)
    journal.unlink(missing_ok=True)
    return LayerCommitResult(CONFLICT, "layer holds foreign/partial bytes; not touched")


def recover_inaudit_dir_commits(audit_dir) -> list[LayerCommitResult]:
    """Resolve every pending save journal in ``audit_dir`` (bounded crash recovery)."""
    directory = Path(audit_dir)
    if not directory.is_dir():
        return []
    results: list[LayerCommitResult] = []
    for entry in directory.iterdir():
        if entry.name.startswith(_JOURNAL_PREFIX) and entry.name.endswith(_JOURNAL_SUFFIX):
            target_name = entry.name[len(_JOURNAL_PREFIX): -len(_JOURNAL_SUFFIX)]
            target = directory / target_name
            if target.exists() or entry.exists():
                result = recover_pending_layer_commit(target)
                if result is not None:
                    results.append(result)
    return results
