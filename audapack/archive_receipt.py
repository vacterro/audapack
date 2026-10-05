"""Canonical archive digest receipts (P1 TARGET D).

WHY THIS EXISTS. `handle_project_archive_ensure` called `ensure_fresh_archive()`,
then `archive.stat()` and `_sha256_path(archive)` -- and it did that even when
the reuse gate answered REUSED_EXISTING, so every Widget ZIP click reread the
entire ZIP to re-derive a digest for bytes that had not changed. The same full
read sat on the download path, which hashed the whole archive before streaming
it. For a multi-hundred-MB archive that is pure, repeated I/O on the operator's
critical path.

WHAT A RECEIPT PROVES. A receipt is written ONLY after a real SHA-256 of the
exact bytes at an exact identity. Its whole job is to answer "are these still
the same bytes?" from cheap metadata rather than by reading them:

    path identity + byte size + mtime_ns + ctime_ns + policy fingerprint

Every field must match, the schema must be current and the recorded digest must
be a well-formed SHA-256, or the receipt is treated as MISSING and the digest is
recomputed and the receipt refreshed atomically. There is no path that returns a
digest the process did not either hash or find already hashed for this exact
identity -- a filename alone never proves anything, and an unproven SHA is never
returned.

SIDECAR LOCATION. Receipts live under the user runtime directory, NOT beside the
archive. The output directory is scanned for archives (`find_archive_for_project`,
`delete_old_archives`, the operator's own file manager); dropping metadata files
into it would put non-archive bytes into that discovery surface. Keying on the
archive's normalized path keeps the store flat and bounded by the number of
archives the operator has actually served.

ARCHIVE IMMUTABILITY CONTRACT AND ITS LIMITS. A receipt is cheap evidence, not a
cryptographic guarantee, and this contract is stated to match what the code
actually enforces:

1. Every sanctioned AUDAPACK archive write creates a new file via atomic replace
   from a ``.part`` staging file (``part_path.replace(output_path)``). On Windows,
   ``os.replace()`` from a new file creates a new ``st_ctime`` (creation time),
   so the cheap identity changes even when size and mtime happen to match.

2. Every sanctioned write path explicitly invalidates any stored receipt for the
   target path (``archive_receipt.invalidate(output_path)``), making that half of
   the contract machine-checkable rather than relying on OS metadata.

That is the whole of the guarantee:

  * all sanctioned AUDAPACK write paths invalidate receipts explicitly;
  * normal metadata-changing external modifications (a copy, a different mtime,
    a resize, a new creation time) invalidate receipts because the identity
    tuple no longer matches.

It is NOT a claim that arbitrary external in-place rewrites are mechanically
detected. ``st_ctime`` on Windows is creation time, so an external writer can
theoretically rewrite same-size bytes in place, preserve the file identity and
even restore ``mtime``; no cheap stat tuple can cryptographically prove that
arbitrary external bytes are unchanged. Such hostile, unsanctioned,
metadata-preserving in-place rewrites are OUTSIDE the zero-read receipt proof --
covering them would require hashing the archive or a separate change monitor,
which is deliberately not paid on the warm path. Receipt safety therefore rests
on sanctioned writes going through the invalidation contract, not on metadata
alone.
"""


from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Callable, Optional

from audapack.config import get_user_runtime_dir

#: Bumped whenever a key a consumer depends on changes meaning. An older receipt
#: is a MISSING receipt, never a partially trusted one.
RECEIPT_SCHEMA = 1

#: Filesystem identity of the archive the receipt describes.
IDENTITY_FIELDS = ("path", "size", "mtime_ns", "ctime_ns")

_HEX_DIGITS = set("0123456789abcdef")


def receipt_dir() -> Path:
    """The runtime directory that holds every archive digest receipt."""
    return get_user_runtime_dir() / "archive_receipts"


def receipt_path_for(archive: Path | str) -> Path:
    """Deterministic receipt path for *archive*'s normalized absolute path.

    Keyed on the resolved path so two spellings of the same archive (relative,
    absolute, mixed case on Windows) share one receipt instead of accumulating
    duplicates that could disagree.
    """
    key = str(_normalized_path(archive)).encode("utf-8", "surrogatepass")
    return receipt_dir() / f"{hashlib.sha256(key).hexdigest()[:32]}.json"


def _normalized_path(value: Path | str) -> Path:
    """The canonical path identity: resolved, and case-folded on Windows."""
    try:
        resolved = Path(value).resolve()
    except OSError:
        resolved = Path(value)
    if os.name == "nt":
        return Path(str(resolved).lower())
    return resolved


def archive_identity(archive: Path | str) -> Optional[dict]:
    """The cheap identity of *archive*, or None when it cannot be read."""
    try:
        stat = Path(archive).stat()
    except OSError:
        return None
    return {
        "path": str(_normalized_path(archive)),
        "size": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
        "ctime_ns": int(stat.st_ctime_ns),
    }


def load_receipt(archive: Path | str) -> Optional[dict]:
    """The stored receipt for *archive*, or None when absent/unreadable."""
    try:
        raw = receipt_path_for(archive).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _digest_is_well_formed(value: object) -> bool:
    text = str(value or "").strip().lower()
    return len(text) == 64 and all(char in _HEX_DIGITS for char in text)


def receipt_digest(archive: Path | str, *, policy_fingerprint: str) -> Optional[str]:
    """The digest *archive*'s receipt proves for THIS exact identity, or None.

    Returns None for every form of "this receipt is not evidence": missing,
    malformed, wrong schema, a different packing policy, a different path, a
    changed size/mtime/ctime, or a digest that is not a SHA-256 at all. The
    caller then has to compute one.
    """
    receipt = load_receipt(archive)
    if receipt is None:
        return None
    if int(receipt.get("schema") or 0) != RECEIPT_SCHEMA:
        return None
    recorded = str(receipt.get("sha256") or "").strip().lower()
    if not _digest_is_well_formed(recorded):
        return None
    if str(receipt.get("policy_fingerprint") or "") != str(policy_fingerprint or ""):
        return None
    identity = archive_identity(archive)
    if identity is None:
        return None
    for field in IDENTITY_FIELDS:
        if receipt.get(field) != identity[field]:
            return None
    return recorded


def store_digest(
    archive: Path | str,
    sha256: str,
    *,
    policy_fingerprint: str,
) -> bool:
    """Atomically record a proven digest for *archive*'s current identity.

    Best-effort by contract: a receipt that cannot be written costs the NEXT
    ensure one extra hash and nothing else, so this never raises a store failure
    into the caller's success path. Validation lives in `receipt_digest`; this
    function only refuses to write evidence that is already unusable (an empty
    policy fingerprint, a malformed digest, an unreadable archive).
    """
    digest = str(sha256 or "").strip().lower()
    if not _digest_is_well_formed(digest):
        return False
    if not str(policy_fingerprint or ""):
        # Without a policy identity the receipt could not be safely reused, so
        # storing it would only produce a file nothing may consume.
        return False
    identity = archive_identity(archive)
    if identity is None:
        return False
    record = {
        "schema": RECEIPT_SCHEMA,
        **identity,
        "sha256": digest,
        "policy_fingerprint": str(policy_fingerprint),
        "recorded_at": time.time(),
    }
    target = receipt_path_for(archive)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=".archive-receipt-", suffix=".tmp", dir=str(target.parent)
        )
    except OSError:
        return False
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record, indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, target)
    except (OSError, ValueError):
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        return False
    return True


def proven_digest(
    archive: Path | str,
    *,
    policy_fingerprint: str,
    compute: Callable[[Path], str],
) -> tuple[str, bool]:
    """The canonical SHA-256 of *archive*, and whether a receipt proved it.

    THE ONLY sanctioned way to ask this question. Order:

        read receipt -> identity matches? -> return it
                                        -> otherwise HASH the bytes, then
                                           refresh the receipt atomically

    `compute` is the caller's hasher (the Bridge passes `_sha256_path`). Its
    exceptions propagate, because a hash that could not be produced is a real
    failure the caller must not paper over with an unproven digest.
    """
    proven = receipt_digest(archive, policy_fingerprint=policy_fingerprint)
    if proven is not None:
        return proven, True
    digest = str(compute(Path(archive)) or "").strip().lower()
    if _digest_is_well_formed(digest):
        store_digest(archive, digest, policy_fingerprint=policy_fingerprint)
    return digest, False


def invalidate(archive: Path | str) -> None:
    """Drop any stored receipt for *archive* (identity changed on purpose)."""
    try:
        receipt_path_for(archive).unlink()
    except OSError:
        pass
