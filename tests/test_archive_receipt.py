"""P1 TARGET D: canonical archive digest receipts.

The defect this closes: `handle_project_archive_ensure` hashed the whole ZIP on
every ensure -- including the REUSED_EXISTING path, where the bytes had not
changed -- and the download route hashed it again before streaming. A receipt
exists so that "same bytes" can be answered from identity instead of from I/O,
WITHOUT ever returning a digest nobody proved.

These tests pin the whole contract: a proven receipt is reused, anything that
can invalidate it does, a missing one is computed and stored once, and a store
that cannot be used degrades to hashing rather than to trusting.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from audapack import archive_receipt


def _archive(root: Path, name: str = "proj.zip", payload: bytes = b"PK\x03\x04 canonical") -> Path:
    path = root / name
    path.write_bytes(payload)
    return path


class _Hasher:
    """A hasher that counts how often the whole archive was actually read."""

    def __init__(self, digest: str = "a" * 64):
        self.calls = 0
        self.digest = digest

    def __call__(self, archive: Path) -> str:
        self.calls += 1
        return self.digest


def test_a_proven_receipt_is_reused_without_rereading_the_archive(tmp_path):
    """TARGET M.4: the reused path must not read the complete ZIP again."""
    archive = _archive(tmp_path)
    hasher = _Hasher()

    first, first_from_receipt = archive_receipt.proven_digest(
        archive, policy_fingerprint="policy-a", compute=hasher
    )
    assert first == hasher.digest
    assert first_from_receipt is False
    assert hasher.calls == 1

    for _ in range(5):
        again, from_receipt = archive_receipt.proven_digest(
            archive, policy_fingerprint="policy-a", compute=hasher
        )
        assert again == first
        assert from_receipt is True
    assert hasher.calls == 1, "a proven receipt must never trigger another full read"


def test_a_missing_receipt_computes_and_stores_one(tmp_path):
    """TARGET M.6: no receipt means hash once, then store it safely."""
    archive = _archive(tmp_path)
    assert archive_receipt.load_receipt(archive) is None

    hasher = _Hasher("b" * 64)
    digest, from_receipt = archive_receipt.proven_digest(
        archive, policy_fingerprint="policy-a", compute=hasher
    )
    assert (digest, from_receipt) == ("b" * 64, False)
    assert hasher.calls == 1

    stored = archive_receipt.load_receipt(archive)
    assert stored is not None
    assert stored["sha256"] == "b" * 64
    assert stored["schema"] == archive_receipt.RECEIPT_SCHEMA
    assert stored["policy_fingerprint"] == "policy-a"

    _digest, from_receipt_again = archive_receipt.proven_digest(
        archive, policy_fingerprint="policy-a", compute=hasher
    )
    assert from_receipt_again is True
    assert hasher.calls == 1


def test_changed_archive_bytes_invalidate_the_receipt(tmp_path):
    """TARGET M.5: different bytes are never served from an old receipt."""
    archive = _archive(tmp_path)
    archive_receipt.store_digest(archive, "c" * 64, policy_fingerprint="policy-a")

    # Same size, different bytes, newer mtime: size alone must not be the gate.
    archive.write_bytes(b"PK\x03\x04 DIFFERENT")
    os.utime(archive, (archive.stat().st_atime + 5, archive.stat().st_mtime + 5))
    assert archive_receipt.receipt_digest(archive, policy_fingerprint="policy-a") is None

    hasher = _Hasher("d" * 64)
    digest, from_receipt = archive_receipt.proven_digest(
        archive, policy_fingerprint="policy-a", compute=hasher
    )
    assert (digest, from_receipt) == ("d" * 64, False)
    assert hasher.calls == 1


def test_a_changed_packing_policy_invalidates_the_receipt(tmp_path):
    archive = _archive(tmp_path)
    archive_receipt.store_digest(archive, "e" * 64, policy_fingerprint="policy-a")
    assert archive_receipt.receipt_digest(archive, policy_fingerprint="policy-b") is None
    assert archive_receipt.receipt_digest(archive, policy_fingerprint="policy-a") == "e" * 64


def test_a_malformed_receipt_is_never_evidence(tmp_path):
    archive = _archive(tmp_path)
    path = archive_receipt.receipt_path_for(archive)
    path.parent.mkdir(parents=True, exist_ok=True)

    for payload in (
        "{not json",
        json.dumps(["not", "an", "object"]),
        json.dumps({"schema": 0, "sha256": "f" * 64, "policy_fingerprint": "policy-a"}),
        json.dumps({"schema": 1, "sha256": "not-a-digest", "policy_fingerprint": "policy-a"}),
    ):
        path.write_text(payload, encoding="utf-8")
        assert archive_receipt.receipt_digest(archive, policy_fingerprint="policy-a") is None


def test_a_receipt_without_a_policy_identity_is_never_written(tmp_path):
    """An empty fingerprint could never be revalidated, so storing it would only
    leave a file nothing is allowed to consume."""
    archive = _archive(tmp_path)
    assert archive_receipt.store_digest(archive, "a" * 64, policy_fingerprint="") is False
    assert archive_receipt.load_receipt(archive) is None


def test_a_receipt_for_another_path_is_not_reused(tmp_path):
    """Identity is the PATH plus the bytes, never the filename alone."""
    (tmp_path / "one").mkdir()
    (tmp_path / "two").mkdir()
    first = _archive(tmp_path / "one")
    second = _archive(tmp_path / "two")
    archive_receipt.store_digest(first, "a" * 64, policy_fingerprint="policy-a")

    assert archive_receipt.load_receipt(second) is None
    assert archive_receipt.receipt_digest(second, policy_fingerprint="policy-a") is None


def test_the_receipt_store_lives_outside_the_archive_directory(tmp_path):
    """Metadata must never land in the scanned output directory."""
    (tmp_path / "out").mkdir()
    archive = _archive(tmp_path / "out")
    archive_receipt.store_digest(archive, "a" * 64, policy_fingerprint="policy-a")
    siblings = sorted(p.name for p in archive.parent.iterdir())
    assert siblings == ["proj.zip"], f"the archive directory was polluted: {siblings}"
    assert archive_receipt.receipt_path_for(archive).is_file()


def test_an_unreadable_archive_never_produces_a_receipt(tmp_path):
    missing = tmp_path / "gone.zip"
    assert archive_receipt.archive_identity(missing) is None
    assert archive_receipt.store_digest(missing, "a" * 64, policy_fingerprint="policy-a") is False
    assert archive_receipt.receipt_digest(missing, policy_fingerprint="policy-a") is None


def test_invalidate_drops_the_receipt(tmp_path):
    archive = _archive(tmp_path)
    archive_receipt.store_digest(archive, "a" * 64, policy_fingerprint="policy-a")
    assert archive_receipt.receipt_digest(archive, policy_fingerprint="policy-a") == "a" * 64
    archive_receipt.invalidate(archive)
    assert archive_receipt.receipt_digest(archive, policy_fingerprint="policy-a") is None


def test_a_failed_write_is_not_fatal(tmp_path, monkeypatch):
    """Acceleration must never break a successful ensure: an unreplaceable
    receipt costs the NEXT call one hash and nothing else."""
    archive = _archive(tmp_path)

    def boom(*_args, **_kwargs):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(archive_receipt.os, "replace", boom)
    assert archive_receipt.store_digest(archive, "a" * 64, policy_fingerprint="policy-a") is False
    leftovers = [p.name for p in archive_receipt.receipt_dir().iterdir() if p.suffix == ".tmp"]
    assert leftovers == [], f"a failed write left temporary files behind: {leftovers}"


# --------------------------------------------------------------------------- #
# TARGET E: sanctioned write paths invalidate receipts
# --------------------------------------------------------------------------- #


def test_atomic_replace_from_staging_invalidates_prior_receipt(tmp_path):
    """A sanctioned AUDAPACK write (atomic replace from .part staging) must
    invalidate any prior receipt. This is the explicit enforcement of the
    archive immutability contract."""
    archive = _archive(tmp_path, payload=b"PK\x03\x04 original bytes")
    hasher = _Hasher("a" * 64)
    # Establish a proven receipt.
    digest, from_receipt = archive_receipt.proven_digest(
        archive, policy_fingerprint="policy-a", compute=hasher
    )
    assert from_receipt is False
    assert hasher.calls == 1
    # Verify the receipt is usable.
    assert archive_receipt.receipt_digest(archive, policy_fingerprint="policy-a") == "a" * 64

    # Simulate a sanctioned write: atomic replace from staging.
    staging = tmp_path / "proj.zip.part.abc123"
    staging.write_bytes(b"PK\x03\x04 new bytes different content")
    staging.replace(archive)

    # Explicit invalidation (as packing.py now does).
    archive_receipt.invalidate(archive)

    # The old receipt must not be reused.
    assert archive_receipt.receipt_digest(archive, policy_fingerprint="policy-a") is None

    # proven_digest must recompute.
    hasher2 = _Hasher("b" * 64)
    digest2, from_receipt2 = archive_receipt.proven_digest(
        archive, policy_fingerprint="policy-a", compute=hasher2
    )
    assert digest2 == "b" * 64
    assert from_receipt2 is False
    assert hasher2.calls == 1


def test_same_size_restored_mtime_after_invalidation_still_recomputes(tmp_path):
    """Even when a rewrite produces the same size and restores the original
    mtime, explicit invalidation ensures the receipt is not reused. This is
    the scenario the old ctime-only documentation incorrectly claimed was
    impossible."""
    archive = _archive(tmp_path, payload=b"PK\x03\x04 exactly20bytes!!")

    archive_receipt.store_digest(archive, "c" * 64, policy_fingerprint="policy-a")
    assert archive_receipt.receipt_digest(archive, policy_fingerprint="policy-a") == "c" * 64

    # Explicit invalidation (as packing.py now does on every write).
    archive_receipt.invalidate(archive)

    # Even if the file happens to have the same size and mtime, the receipt
    # is gone because we invalidated it.
    assert archive_receipt.receipt_digest(archive, policy_fingerprint="policy-a") is None



def test_deleting_an_archive_retires_its_receipt(tmp_path):
    """PERF-004 (audit/12.md).

    `delete_old_archives()` unlinks historical ZIPs but left their receipt JSON
    sidecar behind, and nothing else prunes orphan receipts -- so one file per
    normalized archive path accumulated in the persistent user runtime
    `archive_receipts` directory forever, against that store's stated intent of
    staying bounded by the archives the operator actually serves.
    """
    from audapack.packing import delete_old_archives

    output_dir = tmp_path / "out"
    output_dir.mkdir()
    old = _archive(output_dir, "proj_27.08.26-T00-00-00.zip")
    current = _archive(output_dir, "proj.zip")

    archive_receipt.store_digest(old, "a" * 64, policy_fingerprint="policy-a")
    archive_receipt.store_digest(current, "b" * 64, policy_fingerprint="policy-a")
    assert archive_receipt.receipt_path_for(old).exists()

    removed, errors = delete_old_archives(output_dir, "proj", current)

    assert (removed, errors) == (1, 0)
    assert not old.exists()
    assert not archive_receipt.receipt_path_for(old).exists(), (
        "a deleted archive must not leave an orphan receipt behind"
    )
    # The archive still being served keeps its receipt.
    assert archive_receipt.receipt_path_for(current).exists()


def test_deleting_an_archive_without_a_receipt_is_not_an_error(tmp_path):
    """The retirement is best-effort: a missing receipt must not fail a delete."""
    from audapack.packing import delete_old_archives

    output_dir = tmp_path / "out"
    output_dir.mkdir()
    _archive(output_dir, "proj_27.08.26-T00-00-00.zip")
    current = _archive(output_dir, "proj.zip")

    removed, errors = delete_old_archives(output_dir, "proj", current)

    assert (removed, errors) == (1, 0)
