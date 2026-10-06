"""T-190 (SRC-046) focused tests: packing through the frozen Git inventory.

Proves the WRITE side of the T-190 contract end to end:

- a Git-mode pack archives exactly the frozen inventory
  (tracked_existing UNION untracked_nonignored);
- ``.audapack/manifest.json`` is written, versioned and correct
  (origins, counts, tracked_deleted, per-file SHA-256);
- SHA-256 values are computed from the exact bytes streamed into the ZIP;
- post-write verification covers expected->actual AND actual->expected;
- a failed replacement preserves the previous known-good archive;
- unique ``.part`` staging artifacts are cleaned;
- PackResult carries a truthful Git summary.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
import zipfile
from collections import Counter
from pathlib import Path

from audapack.packing import (
    INVENTORY_MANIFEST_PATH,
    INVENTORY_MANIFEST_SCHEMA_VERSION,
    MANIFEST_FILENAME,
    PACK_STATUS_FAILED_INVENTORY,
    PACK_STATUS_FAILED_VERIFY,
    PACK_STATUS_PACKED,
    pack_single,
)
from audapack.source_inventory import (
    CODE_RESERVED_ARCHIVE_NAME_CONFLICT,
    CODE_TRACKED_HARD_DENY_CONFLICT,
    CODE_TRACKED_RESERVED_ARCHIVE_NAME_CONFLICT,
    ORIGIN_TRACKED,
    ORIGIN_UNTRACKED,
    REASON_SUPERSEDED_GENERATED_CONTROL,
    REASON_TRACKED_DELETED,
    RESERVED_ARCHIVE_NAMES,
    SourceInventoryEntry,
    build_git_inventory,
    reserved_archive_conflict_message,
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "commit.gpgsign=false", *args],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )


class TestGitModePacking(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.repo = self.root / "proj"
        self.repo.mkdir(parents=True)
        _git(self.repo, "init", "-q")
        self.output = self.root / "out"
        self.output.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def _commit_all(self):
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "w")

    def _pack(self, stem="Proj"):
        return pack_single(
            source_path=self.repo,
            output_dir=self.output,
            archive_stem=stem,
            excludes=set(),
            delete_old=True,
            include_timestamp=False,
            manifest_meta={"project_name": stem},
        )

    def test_git_pack_archives_tracked_union_untracked_and_writes_inventory_manifest(self):
        (self.repo / "app.py").write_text("print('x')", encoding="utf-8")
        (self.repo / "media.wav").write_bytes(b"AUDIO" * 8)
        self._commit_all()
        (self.repo / ".gitignore").write_text("ignored.tmp\n", encoding="utf-8")
        self._commit_all()
        (self.repo / "ignored.tmp").write_text("noise", encoding="utf-8")
        victim = self.repo / "gone.py"
        victim.write_text("x", encoding="utf-8")
        self._commit_all()
        victim.unlink()  # tracked worktree deletion
        (self.repo / "new_source.py").write_text("NEW", encoding="utf-8")  # untracked, not ignored

        result = self._pack()
        self.assertTrue(result.success, result.error_message)
        self.assertIn("Git:", result.git_summary)
        with zipfile.ZipFile(result.output_path) as zf:
            names = zf.namelist()
            manifest = json.loads(zf.read(INVENTORY_MANIFEST_PATH))

        self.assertIn("app.py", names)
        self.assertIn("media.wav", names)
        self.assertIn("new_source.py", names)
        self.assertIn("gone.py", manifest["tracked_deleted"])
        self.assertNotIn("ignored.tmp", names)
        self.assertEqual(manifest["schema_version"], INVENTORY_MANIFEST_SCHEMA_VERSION)
        self.assertEqual(manifest["kind"], "audapack_source_inventory")
        self.assertEqual(manifest["inventory_mode"], "git")
        self.assertTrue(manifest["git_head"])
        self.assertEqual(manifest["counts"]["tracked_deleted"], 1)
        by_path = {f["path"]: f for f in manifest["files"]}
        self.assertEqual(by_path["app.py"]["origin"], "tracked")
        self.assertEqual(by_path["new_source.py"]["origin"], "untracked")

    def test_sha256_matches_exact_archived_bytes(self):
        (self.repo / "app.py").write_text("print('x')", encoding="utf-8")
        payload = b"PAYLOAD-" + b"z" * 512
        (self.repo / "blob.bin").write_bytes(payload)
        self._commit_all()

        result = self._pack()
        self.assertTrue(result.success, result.error_message)
        with zipfile.ZipFile(result.output_path) as zf:
            manifest = json.loads(zf.read(INVENTORY_MANIFEST_PATH))
            archived = zf.read("blob.bin")
        self.assertEqual(archived, payload, "archive bytes must equal the streamed source bytes")
        by_path = {f["path"]: f for f in manifest["files"]}
        self.assertEqual(by_path["blob.bin"]["sha256"], hashlib.sha256(payload).hexdigest())
        self.assertEqual(by_path["blob.bin"]["size"], len(payload))

    def test_failed_replacement_preserves_previous_known_good_archive(self):
        (self.repo / "app.py").write_text("v1", encoding="utf-8")
        self._commit_all()
        first = self._pack()
        self.assertTrue(first.success)
        before = first.output_path.read_bytes()

        # A tracked hard-safety conflict fails the NEXT pack closed.
        secret = self.repo / "token.txt"
        secret.write_text("SECRET", encoding="utf-8")
        self._commit_all()
        second = self._pack()
        self.assertFalse(second.success)
        self.assertEqual(
            first.output_path.read_bytes(),
            before,
            "the previous known-good archive must survive a failed replacement",
        )

    def test_staging_part_artifacts_are_cleaned_on_failure(self):
        (self.repo / "app.py").write_text("v1", encoding="utf-8")
        self._commit_all()
        secret = self.repo / "token.txt"
        secret.write_text("SECRET", encoding="utf-8")
        self._commit_all()
        result = self._pack()
        self.assertFalse(result.success)
        parts = list(self.output.glob("*.part.*"))
        self.assertEqual(parts, [], f"staging artifacts leaked: {parts}")

    def test_fs_mode_pack_also_carries_inventory_manifest(self):
        plain = self.root / "plain"
        plain.mkdir()
        (plain / "a.txt").write_text("A", encoding="utf-8")
        result = pack_single(
            source_path=plain,
            output_dir=self.output,
            archive_stem="Plain",
            excludes=set(),
            delete_old=True,
            include_timestamp=False,
            manifest_meta={"project_name": "Plain"},
        )
        self.assertTrue(result.success, result.error_message)
        with zipfile.ZipFile(result.output_path) as zf:
            manifest = json.loads(zf.read(INVENTORY_MANIFEST_PATH))
            names = zf.namelist()
        self.assertEqual(manifest["inventory_mode"], "filesystem")
        by_path = {f["path"]: f for f in manifest["files"]}
        self.assertEqual(by_path["a.txt"]["origin"], "filesystem")
        self.assertIn(MANIFEST_FILENAME, names)
        self.assertIn(INVENTORY_MANIFEST_PATH, names)

    def test_tracked_deleted_reason_recorded_in_inventory_entries(self):
        (self.repo / "gone.py").write_text("x", encoding="utf-8")
        self._commit_all()
        (self.repo / "gone.py").unlink()
        from audapack.source_inventory import build_git_inventory

        inv = build_git_inventory(self.repo, set())
        excluded = {e.rel: e for e in inv.excluded_entries()}
        self.assertEqual(excluded["gone.py"].reason, REASON_TRACKED_DELETED)

class TestProtectedControlPlaneEndToEnd(unittest.TestCase):
    """T-188 P0: protected control plane must survive the full Git-mode pack.

    Regression A (archive membership + accounting + SHA) and D (accounting
    identity) from the audit-core handoff, proven through the production
    ``pack_single`` path.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.repo = self.root / "proj"
        self.repo.mkdir(parents=True)
        _git(self.repo, "init", "-q")
        self.output = self.root / "out"
        self.output.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def _pack(self, stem="Proj"):
        return pack_single(
            source_path=self.repo,
            output_dir=self.output,
            archive_stem=stem,
            excludes=set(),
            delete_old=True,
            include_timestamp=False,
            manifest_meta={"project_name": stem},
        )

    def test_ignored_protected_control_plane_reaches_archive_with_honest_accounting(self):
        (self.repo / "app.py").write_text("print('x')", encoding="utf-8")
        (self.repo / ".gitignore").write_text(
            ".saipen/intake/\naudit/\n.saipen/recovery/\n", encoding="utf-8"
        )
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "init")

        intake_payload = "SRC-044 RECEIPT BYTES"
        intake = self.repo / ".saipen" / "intake" / "active"
        intake.mkdir(parents=True)
        (intake / "SRC-044.md").write_text(intake_payload, encoding="utf-8")
        audit_payload = "AUDIT LAYER TEN BYTES"
        audit = self.repo / "audit"
        audit.mkdir()
        (audit / "10.md").write_text(audit_payload, encoding="utf-8")
        recovery = self.repo / ".saipen" / "recovery" / "deep"
        recovery.mkdir(parents=True)
        for i in range(12):
            (recovery / f"junk{i}.bin").write_bytes(b"x" * 64)

        result = self._pack()
        self.assertTrue(result.success, result.error_message)
        self.assertEqual(
            result.files_discovered,
            result.files_included + result.files_excluded + result.files_failed,
            "accounting must stay reconciled with the protected overlay",
        )

        with zipfile.ZipFile(result.output_path) as zf:
            names = zf.namelist()
            manifest = json.loads(zf.read(INVENTORY_MANIFEST_PATH))
            archived_intake = zf.read(".saipen/intake/active/SRC-044.md")
            archived_audit = zf.read("audit/10.md")

        self.assertEqual(archived_intake, intake_payload.encode("utf-8"))
        self.assertEqual(archived_audit, audit_payload.encode("utf-8"))
        by_path = {f["path"]: f for f in manifest["files"]}
        self.assertEqual(
            by_path[".saipen/intake/active/SRC-044.md"]["sha256"],
            hashlib.sha256(intake_payload.encode("utf-8")).hexdigest(),
        )
        self.assertEqual(
            by_path[".saipen/intake/active/SRC-044.md"]["size"], len(intake_payload)
        )
        self.assertEqual(
            by_path["audit/10.md"]["sha256"],
            hashlib.sha256(audit_payload.encode("utf-8")).hexdigest(),
        )
        self.assertEqual(
            by_path["audit/10.md"]["size"], len(audit_payload)
        )
        # Git ignore alone is never an exclusion for protected material.
        self.assertNotIn("git_ignored", {f.get("origin") for f in manifest["files"]})
        # Recovery tree stays out and stays honestly uncounted as included.
        self.assertFalse(any(n.startswith(".saipen/recovery/") for n in names))

    def test_protected_overlay_files_are_counted_in_manifest_counts(self):
        (self.repo / "app.py").write_text("print('x')", encoding="utf-8")
        (self.repo / ".gitignore").write_text("audit/\n", encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "init")
        (self.repo / "audit").mkdir()
        (self.repo / "audit" / "10.md").write_text("LAYER", encoding="utf-8")

        result = self._pack()
        self.assertTrue(result.success, result.error_message)
        with zipfile.ZipFile(result.output_path) as zf:
            manifest = json.loads(zf.read(INVENTORY_MANIFEST_PATH))
        counts = manifest["counts"]
        files = manifest["files"]
        self.assertEqual(
            counts["included"], len(files), "manifest counts must match file ledger"
        )
        self.assertIn("audit/10.md", {f["path"] for f in files})
        self.assertEqual(
            result.files_discovered,
            result.files_included + result.files_excluded + result.files_failed,
        )


class TestReservedArchiveControlCollision(unittest.TestCase):
    """Reserved archive-control names are a COLLISION, not a secret (SRC-046).

    Measured failure (real Project Room, ``_ZAICODE``): a Git-tracked
    ``_AUDAPACK_MANIFEST.json`` -- a path AUDAPACK itself writes as generated
    archive metadata -- failed the pack as
    ``[FAILED_INVENTORY][TRACKED_HARD_DENY_CONFLICT] tracked path matches the
    hard-safety deny policy``. The refusal was right; the label sent the
    operator hunting for a credential that was never there.

    These tests pin the whole regression matrix end to end through the real
    ``pack_single`` path: fail-closed with the reserved-name classification and
    NO archive byte, the operator message naming path + rule + repair, secrets
    unchanged, an untracked physical copy omitted rather than duplicated, and
    exactly one AUDAPACK manifest per archive.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.output = self.root / "out"
        self.output.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def _new_repo(self, name: str) -> Path:
        repo = self.root / name
        repo.mkdir(parents=True, exist_ok=True)
        _git(repo, "init", "-q")
        return repo

    def _track(self, repo: Path, rel: str, text: str = "{}") -> None:
        path = repo.joinpath(*rel.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "w")

    def _pack_repo(self, repo: Path, stem: str = "Proj", manifest: bool = True):
        return pack_single(
            source_path=repo,
            output_dir=self.output,
            archive_stem=stem,
            excludes=set(),
            delete_old=True,
            include_timestamp=False,
            manifest_meta={"project_name": stem} if manifest else None,
        )

    def test_tracked_reserved_path_fails_closed_and_names_the_collision(self):
        for index, rel in enumerate(sorted(RESERVED_ARCHIVE_NAMES)):
            with self.subTest(rel=rel):
                repo = self._new_repo(f"repo{index}")
                self._track(repo, "app.py", "print('x')")
                self._track(repo, rel)

                result = self._pack_repo(repo, stem=f"Stem{index}")

                # 1. Fail closed: no successful result, and no archive byte for
                #    this pack. The tracked file is neither silently omitted nor
                #    overwritten -- the operator decides.
                self.assertFalse(result.success)
                self.assertEqual(result.status, PACK_STATUS_FAILED_INVENTORY)
                self.assertEqual(
                    result.error_code, CODE_TRACKED_RESERVED_ARCHIVE_NAME_CONFLICT
                )
                self.assertEqual(result.first_error_path, rel)
                self.assertEqual(sorted(self.output.glob("*.zip")), [])

                # 2. Exact machine classification, then the actionable message:
                #    the conflicting relative path, what it collides with, and
                #    the source-control repair.
                self.assertEqual(
                    result.error_message,
                    f"[{CODE_TRACKED_RESERVED_ARCHIVE_NAME_CONFLICT}] "
                    f"{reserved_archive_conflict_message(rel)} ({rel})",
                )
                self.assertNotIn("hard-safety", result.error_message)

    def test_tracked_reserved_conflict_preserves_previous_archive(self):
        repo = self._new_repo("prev")
        self._track(repo, "app.py", "v1")
        first = self._pack_repo(repo, stem="Prev")
        self.assertTrue(first.success, first.error_message)
        before = first.output_path.read_bytes()

        self._track(repo, "_AUDAPACK_MANIFEST.json")
        second = self._pack_repo(repo, stem="Prev")

        self.assertFalse(second.success)
        self.assertEqual(second.error_code, CODE_TRACKED_RESERVED_ARCHIVE_NAME_CONFLICT)
        self.assertEqual(
            first.output_path.read_bytes(),
            before,
            "a reserved-name refusal must leave the last good archive untouched",
        )

    def test_tracked_secret_stays_hard_safety(self):
        repo = self._new_repo("key")
        self._track(repo, "app.py", "print('x')")
        self._track(repo, "id_rsa", "PRIVATE")

        result = self._pack_repo(repo, stem="Key")

        self.assertFalse(result.success)
        self.assertEqual(result.status, PACK_STATUS_FAILED_INVENTORY)
        self.assertEqual(result.error_code, CODE_TRACKED_HARD_DENY_CONFLICT)
        self.assertEqual(result.first_error_path, "id_rsa")
        self.assertIn("hard-safety deny policy", result.error_message)
        self.assertNotIn(CODE_TRACKED_RESERVED_ARCHIVE_NAME_CONFLICT, result.error_message)

    def test_untracked_unrecognized_manifest_fails_closed_like_its_tracked_twin(self):
        """SRC-100: the untracked leg no longer has a laxer reserved-name rule.

        This test used to assert that an untracked ``_AUDAPACK_MANIFEST.json``
        holding ``{"stale": true}`` was silently omitted and the pack went
        ahead. That is the same origin-blindness that let a control artifact be
        written twice, seen from its permissive side: the TRACKED twin of this
        exact content class (``test_project_owned_file_on_reserved_name_still_
        fails_closed``) has always failed closed, so a reserved name meant two
        different things depending on whether Git happened to know about it.

        Silently omitting the file is not a safe middle ground either -- it is
        the omission T-190 exists to prevent, and it drops project bytes with
        no record. Unrecognized content on a name AUDAPACK owns now fails
        closed in both origins, and says which origin it saw.
        """
        repo = self._new_repo("phys")
        self._track(repo, "app.py", "print('x')")
        (repo / MANIFEST_FILENAME).write_text('{"stale": true}', encoding="utf-8")

        result = self._pack_repo(repo, stem="Phys")

        self.assertFalse(result.success)
        self.assertEqual(result.status, PACK_STATUS_FAILED_INVENTORY)
        self.assertEqual(result.error_code, CODE_RESERVED_ARCHIVE_NAME_CONFLICT)
        self.assertEqual(result.first_error_path, MANIFEST_FILENAME)
        # Fail closed BEFORE any archive byte exists: there is no output to
        # ship, let alone one carrying project bytes under a control name. The
        # recognized case is the one that gets a fresh artifact instead, proven
        # separately in TestUntrackedGeneratedControlSupersession.
        self.assertIsNone(result.output_path)
        # The source is untouched by a refusal: nothing was renamed or removed
        # on the operator's behalf.
        self.assertEqual(
            (repo / MANIFEST_FILENAME).read_text(encoding="utf-8"),
            '{"stale": true}',
        )


class TestTrackedGeneratedControlSupersession(unittest.TestCase):
    """T-246: a tracked AUDAPACK-generated control artifact is superseded.

    The reserved-name refusal exists so the packer can neither silently omit a
    tracked path nor emit two members under one archive-control name. But a
    project that committed its OWN manifest -- the exact state the refusal
    blocks -- can then never be packed again without a human running
    ``git rm --cached`` by hand. The fix supersedes that artifact: the fresh
    control artifact replaces it, and the omission is named.

    Safety rails pinned here, because the alternative is the failure T-190
    exists to prevent: only a reserved name this pack regenerates is a
    candidate, only bytes carrying AUDAPACK's own marker qualify, the worktree
    is never modified, and anything else keeps the fail-closed refusal.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.output = self.root / "out"
        self.output.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def _new_repo(self, name: str) -> Path:
        repo = self.root / name
        repo.mkdir(parents=True, exist_ok=True)
        _git(repo, "init", "-q")
        return repo

    def _track(self, repo: Path, rel: str, data: bytes = b"{}") -> None:
        path = repo.joinpath(*rel.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "w")

    def _pack_repo(self, repo: Path, stem: str = "Proj", manifest: bool = True):
        return pack_single(
            source_path=repo,
            output_dir=self.output,
            archive_stem=stem,
            excludes=set(),
            delete_old=True,
            include_timestamp=False,
            manifest_meta={"project_name": stem} if manifest else None,
        )

    def _generated_bytes(self, repo: Path, rel: str, stem: str) -> bytes:
        """Real AUDAPACK output, not a hand-forged marker: pack once, read it."""
        first = self._pack_repo(repo, stem=stem)
        self.assertTrue(first.success, first.error_message)
        with zipfile.ZipFile(first.output_path) as zf:
            return zf.read(rel)

    def test_tracked_generated_artifact_is_superseded_and_pack_succeeds(self):
        for index, rel in enumerate(sorted(RESERVED_ARCHIVE_NAMES)):
            with self.subTest(rel=rel):
                repo = self._new_repo(f"sup{index}")
                self._track(repo, "app.py", b"print('x')")
                stale = self._generated_bytes(repo, rel, stem=f"Sup{index}")
                self._track(repo, rel, stale)

                result = self._pack_repo(repo, stem=f"Sup{index}")

                # 1. The pack now succeeds -- the operator trap is gone.
                self.assertTrue(result.success, result.error_message)
                self.assertEqual(result.status, "PACKED")

                with zipfile.ZipFile(result.output_path) as zf:
                    names = zf.namelist()
                    fresh = zf.read(rel)
                    inventory = json.loads(zf.read(INVENTORY_MANIFEST_PATH))

                # 2. Exactly one member under the reserved name, and it is the
                #    artifact this pack generated -- not the tracked stale copy.
                self.assertEqual([n for n in names if n == rel], [rel])
                self.assertNotEqual(fresh, stale)

                # 3. The omission is NAMED, never a silent hole.
                self.assertEqual(inventory["superseded_control"], [rel])
                self.assertNotIn(rel, {f["path"] for f in inventory["files"]})

                # 4. The worktree is untouched: AUDAPACK never edits source.
                self.assertEqual(repo.joinpath(*rel.split("/")).read_bytes(), stale)

    def test_project_owned_file_on_reserved_name_still_fails_closed(self):
        """The safety rail: a reserved name alone never earns supersession."""
        for index, rel in enumerate(sorted(RESERVED_ARCHIVE_NAMES)):
            with self.subTest(rel=rel):
                repo = self._new_repo(f"owned{index}")
                self._track(repo, "app.py", b"print('x')")
                self._track(repo, rel, b'{"project": "hand written"}')

                result = self._pack_repo(repo, stem=f"Owned{index}")

                self.assertFalse(result.success)
                self.assertEqual(result.status, PACK_STATUS_FAILED_INVENTORY)
                self.assertEqual(
                    result.error_code, CODE_TRACKED_RESERVED_ARCHIVE_NAME_CONFLICT
                )
                self.assertEqual(result.first_error_path, rel)

    def test_unparseable_reserved_file_still_fails_closed(self):
        """A marker alone is not proof -- the bytes must parse as the artifact."""
        repo = self._new_repo("broken")
        self._track(repo, "app.py", b"print('x')")
        self._track(repo, MANIFEST_FILENAME, b'{"product": "AUDAPACK" truncated')

        result = self._pack_repo(repo, stem="Broken")

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, CODE_TRACKED_RESERVED_ARCHIVE_NAME_CONFLICT)

    def test_reserved_name_not_regenerated_by_this_pack_is_not_superseded(self):
        """With the legacy manifest disabled this pack writes no such artifact.

        Superseding there would drop a tracked file from the archive without
        replacing it, so the refusal stands until the pack actually regenerates
        the name. The inventory manifest is always written, so it supersedes.
        """
        repo = self._new_repo("off")
        self._track(repo, "app.py", b"print('x')")
        self._track(repo, MANIFEST_FILENAME, self._generated_bytes(repo, MANIFEST_FILENAME, "Off"))

        result = self._pack_repo(repo, stem="Off", manifest=False)

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, CODE_TRACKED_RESERVED_ARCHIVE_NAME_CONFLICT)

        repo2 = self._new_repo("on")
        self._track(repo2, "app.py", b"print('x')")
        self._track(
            repo2,
            INVENTORY_MANIFEST_PATH,
            self._generated_bytes(repo2, INVENTORY_MANIFEST_PATH, "On"),
        )

        result2 = self._pack_repo(repo2, stem="On", manifest=False)

        self.assertTrue(result2.success, result2.error_message)
        with zipfile.ZipFile(result2.output_path) as zf:
            inventory = json.loads(zf.read(INVENTORY_MANIFEST_PATH))
        self.assertEqual(inventory["superseded_control"], [INVENTORY_MANIFEST_PATH])

    def test_omitted_entry_counts_as_an_exclusion(self):
        repo = self._new_repo("reason")
        self._track(repo, "app.py", b"print('x')")
        stale = self._generated_bytes(repo, MANIFEST_FILENAME, "Reason")
        self._track(repo, MANIFEST_FILENAME, stale)

        # The frozen entry itself carries the reason, so the omission is named
        # at the inventory level and not only in the published manifest.
        inventory = build_git_inventory(
            repo, set(), supersedable_reserved=RESERVED_ARCHIVE_NAMES
        )
        entry = inventory.entries[MANIFEST_FILENAME]
        self.assertFalse(entry.include)
        self.assertEqual(entry.reason, REASON_SUPERSEDED_GENERATED_CONTROL)
        self.assertEqual(inventory.superseded_control, [MANIFEST_FILENAME])

        result = self._pack_repo(repo, stem="Reason")

        self.assertTrue(result.success, result.error_message)
        with zipfile.ZipFile(result.output_path) as zf:
            published = json.loads(zf.read(INVENTORY_MANIFEST_PATH))
        # The superseded path is counted as an exclusion, never as included.
        self.assertEqual(published["counts"]["excluded"], 1)
        self.assertEqual(published["counts"]["included"], 1)

    def test_symlink_on_reserved_name_is_not_read_through(self):
        """A link is never followed to decide membership; the refusal stands."""
        repo = self._new_repo("link")
        self._track(repo, "app.py", b"print('x')")
        outside = self.root / "outside.json"
        outside.write_text('{"product": "AUDAPACK"}', encoding="utf-8")
        try:
            repo.joinpath(MANIFEST_FILENAME).symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable on this platform")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "w")

        result = self._pack_repo(repo, stem="Link")

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, CODE_TRACKED_RESERVED_ARCHIVE_NAME_CONFLICT)

    def test_supersession_never_weakens_hard_safety(self):
        repo = self._new_repo("secret")
        self._track(repo, "app.py", b"print('x')")
        self._track(repo, "id_rsa", b"PRIVATE")

        result = self._pack_repo(repo, stem="Secret")

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, CODE_TRACKED_HARD_DENY_CONFLICT)


class TestUntrackedGeneratedControlSupersession(unittest.TestCase):
    """SRC-100: the tracked supersession was asymmetric -- UNTRACKED had none.

    T-246 superseded a TRACKED AUDAPACK-generated control artifact, because a
    project that committed its own manifest could otherwise never be packed
    again without a human running ``git rm --cached``. The untracked leg kept
    none of that: ``build_git_inventory``'s untracked loop never looked at
    ``RESERVED_ARCHIVE_NAMES`` at all, so an untracked, non-ignored
    ``.audapack/manifest.json`` -- the ordinary state of a project that packed
    once and never committed the result -- was classified as ordinary JSON
    payload, entered ``included_entries()``, and was written by the payload
    writer and then AGAIN by ``build_inventory_manifest_payload()``.

    Post-write parity verification correctly rejected the archive with
    "archive member written more than once" and a bare
    ``Duplicate name: '.audapack/manifest.json'``: a real production pack
    failure whose message named neither the cause nor the file.

    The invariant is ONE WRITER PER CONTROL PATH, and it is origin-blind: a
    control artifact AUDAPACK generated is the same artifact whether Git
    happens to track it or not. These tests pin that, and pin the rail that
    must survive it -- arbitrary project-owned bytes on a reserved name still
    fail closed, and AUDAPACK never packages a file it does not own.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.output = self.root / "out"
        self.output.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def _new_repo(self, name: str) -> Path:
        repo = self.root / name
        repo.mkdir(parents=True, exist_ok=True)
        _git(repo, "init", "-q")
        return repo

    def _track(self, repo: Path, rel: str, data: bytes = b"{}") -> None:
        path = repo.joinpath(*rel.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "w")

    def _place_untracked(self, repo: Path, rel: str, data: bytes) -> Path:
        """Write a file Git will report as untracked and non-ignored.

        No ``git add`` and no ignore rule: this is the state a project is in
        after one successful pack whose control artifact it never committed.
        """
        path = repo.joinpath(*rel.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def _pack_repo(self, repo: Path, stem: str = "Proj", manifest: bool = True):
        return pack_single(
            source_path=repo,
            output_dir=self.output,
            archive_stem=stem,
            excludes=set(),
            delete_old=True,
            include_timestamp=False,
            manifest_meta={"project_name": stem} if manifest else None,
        )

    def _generated_bytes(self, repo: Path, rel: str, stem: str) -> bytes:
        """Real AUDAPACK output, not a hand-forged marker: pack once, read it."""
        first = self._pack_repo(repo, stem=stem)
        self.assertTrue(first.success, first.error_message)
        with zipfile.ZipFile(first.output_path) as zf:
            return zf.read(rel)

    def _assert_single_members(self, archive: Path) -> None:
        with zipfile.ZipFile(archive) as zf:
            counts = Counter(zf.namelist())
        duplicates = {name: n for name, n in counts.items() if n > 1}
        self.assertEqual(duplicates, {}, f"archive carries duplicate members: {duplicates}")

    # --- the reproduction ----------------------------------------------------

    def test_untracked_generated_inventory_manifest_is_superseded(self):
        repo = self._new_repo("untracked_inventory")
        self._track(repo, "app.py", b"print('x')")
        stale = self._generated_bytes(repo, INVENTORY_MANIFEST_PATH, "Untracked")
        path = self._place_untracked(repo, INVENTORY_MANIFEST_PATH, stale)

        # The exact production precondition: untracked, non-ignored.
        status = subprocess.run(
            ["git", "status", "--porcelain", "--", INVENTORY_MANIFEST_PATH],
            cwd=str(repo), capture_output=True, text=True, check=True,
        ).stdout
        self.assertTrue(status.startswith("??"), f"must be untracked, got {status!r}")

        result = self._pack_repo(repo, stem="Untracked")

        self.assertTrue(result.success, result.error_message)
        self.assertEqual(result.status, PACK_STATUS_PACKED)
        self._assert_single_members(result.output_path)
        with zipfile.ZipFile(result.output_path) as zf:
            published = json.loads(zf.read(INVENTORY_MANIFEST_PATH))
            self.assertEqual(
                [n for n in zf.namelist() if n == INVENTORY_MANIFEST_PATH],
                [INVENTORY_MANIFEST_PATH],
            )
        self.assertEqual(published["superseded_control"], [INVENTORY_MANIFEST_PATH])
        # The source artifact is EXCLUDED evidence, never included payload.
        self.assertNotIn(INVENTORY_MANIFEST_PATH, {f["path"] for f in published["files"]})
        self.assertIn("app.py", {f["path"] for f in published["files"]})
        # AUDAPACK never edits source.
        self.assertEqual(path.read_bytes(), stale)

    def test_untracked_generated_legacy_manifest_is_superseded(self):
        repo = self._new_repo("untracked_legacy")
        self._track(repo, "app.py", b"print('x')")
        stale = self._generated_bytes(repo, MANIFEST_FILENAME, "Legacy")
        self._place_untracked(repo, MANIFEST_FILENAME, stale)

        result = self._pack_repo(repo, stem="Legacy")

        self.assertTrue(result.success, result.error_message)
        self._assert_single_members(result.output_path)
        with zipfile.ZipFile(result.output_path) as zf:
            published = json.loads(zf.read(INVENTORY_MANIFEST_PATH))
        self.assertIn(MANIFEST_FILENAME, published["superseded_control"])
        self.assertNotIn(MANIFEST_FILENAME, {f["path"] for f in published["files"]})

    def test_untracked_generated_entry_keeps_untracked_origin_and_frozen_evidence(self):
        repo = self._new_repo("evidence")
        self._track(repo, "app.py", b"print('x')")
        stale = self._generated_bytes(repo, INVENTORY_MANIFEST_PATH, "Evidence")
        path = self._place_untracked(repo, INVENTORY_MANIFEST_PATH, stale)
        stat = path.stat()

        inventory = build_git_inventory(
            repo, set(), supersedable_reserved=RESERVED_ARCHIVE_NAMES
        )
        entry = inventory.entries[INVENTORY_MANIFEST_PATH]

        self.assertFalse(entry.include)
        self.assertEqual(entry.reason, REASON_SUPERSEDED_GENERATED_CONTROL)
        self.assertEqual(entry.origin, ORIGIN_UNTRACKED,
            "supersession must not rewrite where the file actually came from")
        self.assertEqual(inventory.superseded_control, [INVENTORY_MANIFEST_PATH])
        # Frozen source evidence, not a bare placeholder: the superseded file
        # stays auditable instead of vanishing as a size-0 ghost.
        self.assertEqual(entry.size, stat.st_size)
        self.assertNotEqual(entry.size, 0)
        self.assertEqual(entry.mtime_ns, stat.st_mtime_ns)
        self.assertEqual(entry.st_ino, stat.st_ino)
        self.assertNotIn(INVENTORY_MANIFEST_PATH, inventory.included_entries())

    def test_both_generated_controls_untracked_supersede(self):
        repo = self._new_repo("both_untracked")
        self._track(repo, "app.py", b"print('x')")
        first = self._pack_repo(repo, stem="Both")
        self.assertTrue(first.success, first.error_message)
        with zipfile.ZipFile(first.output_path) as zf:
            inventory_bytes = zf.read(INVENTORY_MANIFEST_PATH)
            manifest_bytes = zf.read(MANIFEST_FILENAME)
        self._place_untracked(repo, INVENTORY_MANIFEST_PATH, inventory_bytes)
        self._place_untracked(repo, MANIFEST_FILENAME, manifest_bytes)

        result = self._pack_repo(repo, stem="Both")

        self.assertTrue(result.success, result.error_message)
        self._assert_single_members(result.output_path)
        with zipfile.ZipFile(result.output_path) as zf:
            published = json.loads(zf.read(INVENTORY_MANIFEST_PATH))
        self.assertEqual(
            sorted(published["superseded_control"]),
            sorted([INVENTORY_MANIFEST_PATH, MANIFEST_FILENAME]),
        )

    def test_mixed_tracked_and_untracked_generated_controls_supersede(self):
        repo = self._new_repo("mixed")
        self._track(repo, "app.py", b"print('x')")
        first = self._pack_repo(repo, stem="Mixed")
        self.assertTrue(first.success, first.error_message)
        with zipfile.ZipFile(first.output_path) as zf:
            inventory_bytes = zf.read(INVENTORY_MANIFEST_PATH)
            manifest_bytes = zf.read(MANIFEST_FILENAME)
        self._track(repo, INVENTORY_MANIFEST_PATH, inventory_bytes)
        self._place_untracked(repo, MANIFEST_FILENAME, manifest_bytes)

        inventory = build_git_inventory(
            repo, set(), supersedable_reserved=RESERVED_ARCHIVE_NAMES
        )
        self.assertEqual(
            inventory.entries[INVENTORY_MANIFEST_PATH].origin, ORIGIN_TRACKED
        )
        self.assertEqual(
            inventory.entries[MANIFEST_FILENAME].origin, ORIGIN_UNTRACKED
        )

        result = self._pack_repo(repo, stem="Mixed")

        self.assertTrue(result.success, result.error_message)
        self._assert_single_members(result.output_path)

    # --- the rail that must survive -----------------------------------------

    def test_untracked_project_owned_reserved_content_fails_closed(self):
        """A reserved name alone never earns supersession, untracked either.

        Packaging arbitrary project-owned bytes under a name AUDAPACK itself
        writes would let a project smuggle content into generated archive
        metadata, so unrecognized content on a control name is a refusal --
        with a classification that does not lie about where it came from.
        """
        for index, rel in enumerate(sorted(RESERVED_ARCHIVE_NAMES)):
            with self.subTest(rel=rel):
                repo = self._new_repo(f"untracked_owned{index}")
                self._track(repo, "app.py", b"print('x')")
                self._place_untracked(repo, rel, b'{"project": "hand written"}')

                result = self._pack_repo(repo, stem=f"UntrackedOwned{index}")

                self.assertFalse(result.success)
                self.assertEqual(result.status, PACK_STATUS_FAILED_INVENTORY)
                self.assertNotEqual(
                    result.error_code, CODE_TRACKED_RESERVED_ARCHIVE_NAME_CONFLICT,
                    "an untracked file must not be reported as a tracked path",
                )
                self.assertEqual(
                    result.error_code, CODE_RESERVED_ARCHIVE_NAME_CONFLICT
                )
                self.assertEqual(result.first_error_path, rel)

    def test_untracked_malformed_reserved_json_fails_closed(self):
        """A marker alone is not proof -- the bytes must parse as the artifact."""
        for index, rel in enumerate(sorted(RESERVED_ARCHIVE_NAMES)):
            with self.subTest(rel=rel):
                repo = self._new_repo(f"untracked_broken{index}")
                self._track(repo, "app.py", b"print('x')")
                self._place_untracked(repo, rel, b'{"product": "AUDAPACK" truncated')

                result = self._pack_repo(repo, stem=f"UntrackedBroken{index}")

                self.assertFalse(result.success)
                self.assertEqual(result.error_code, CODE_RESERVED_ARCHIVE_NAME_CONFLICT)

    def test_untracked_reserved_name_not_regenerated_by_this_pack_is_refused(self):
        """With the legacy manifest off this pack writes no such artifact.

        Superseding there would drop real content from the archive without
        replacing it, so the refusal stands until the pack regenerates the name.
        """
        repo = self._new_repo("untracked_off")
        self._track(repo, "app.py", b"print('x')")
        stale = self._generated_bytes(repo, MANIFEST_FILENAME, "Off")
        self._place_untracked(repo, MANIFEST_FILENAME, stale)

        result = self._pack_repo(repo, stem="Off", manifest=False)

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, CODE_RESERVED_ARCHIVE_NAME_CONFLICT)

    def test_ignored_generated_control_is_not_in_inventory(self):
        """An ignored control never enters the inventory, so nothing changes."""
        repo = self._new_repo("ignored")
        self._track(repo, "app.py", b"print('x')")
        self._track(repo, ".gitignore", b".audapack/\n")
        stale = self._generated_bytes(repo, INVENTORY_MANIFEST_PATH, "Ignored")
        self._place_untracked(repo, INVENTORY_MANIFEST_PATH, stale)

        inventory = build_git_inventory(
            repo, set(), supersedable_reserved=RESERVED_ARCHIVE_NAMES
        )
        self.assertNotIn(INVENTORY_MANIFEST_PATH, inventory.entries)

        result = self._pack_repo(repo, stem="Ignored")

        self.assertTrue(result.success, result.error_message)
        self._assert_single_members(result.output_path)

    def test_previous_good_archive_survives_a_failed_untracked_collision(self):
        """A refusal must not cost the operator the archive they already had."""
        repo = self._new_repo("survives")
        self._track(repo, "app.py", b"print('x')")
        good = self._pack_repo(repo, stem="Survives")
        self.assertTrue(good.success, good.error_message)
        kept = good.output_path.read_bytes()

        self._place_untracked(repo, MANIFEST_FILENAME, b'{"project": "hand written"}')
        failed = self._pack_repo(repo, stem="Survives")

        self.assertFalse(failed.success)
        self.assertTrue(good.output_path.exists(), "the previous good archive must survive")
        self.assertEqual(good.output_path.read_bytes(), kept)

    def test_source_worktree_and_git_state_are_untouched(self):
        repo = self._new_repo("untouched")
        self._track(repo, "app.py", b"print('x')")
        stale = self._generated_bytes(repo, INVENTORY_MANIFEST_PATH, "Untouched")
        path = self._place_untracked(repo, INVENTORY_MANIFEST_PATH, stale)
        before_status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=str(repo), capture_output=True, text=True, check=True
        ).stdout
        before_index = subprocess.run(
            ["git", "ls-files", "--stage"], cwd=str(repo), capture_output=True, text=True, check=True
        ).stdout
        before_bytes = path.read_bytes()

        result = self._pack_repo(repo, stem="Untouched")

        self.assertTrue(result.success, result.error_message)
        self.assertEqual(path.read_bytes(), before_bytes, "AUDAPACK never edits source")
        self.assertEqual(
            subprocess.run(
                ["git", "status", "--porcelain"], cwd=str(repo), capture_output=True, text=True, check=True
            ).stdout,
            before_status,
            "git status must be unchanged by a pack",
        )
        self.assertEqual(
            subprocess.run(
                ["git", "ls-files", "--stage"], cwd=str(repo), capture_output=True, text=True, check=True
            ).stdout,
            before_index,
            "the git index must be unchanged by a pack",
        )

    def test_superseded_control_counts_as_exclusion_not_inclusion(self):
        repo = self._new_repo("counts")
        self._track(repo, "app.py", b"print('x')")
        stale = self._generated_bytes(repo, INVENTORY_MANIFEST_PATH, "Counts")
        self._place_untracked(repo, INVENTORY_MANIFEST_PATH, stale)

        result = self._pack_repo(repo, stem="Counts")
        self.assertTrue(result.success, result.error_message)

        with zipfile.ZipFile(result.output_path) as zf:
            published = json.loads(zf.read(INVENTORY_MANIFEST_PATH))
        counts = published["counts"]
        # Discovered and excluded, never included. The generated replacement
        # is archive metadata, not source material, and must not be counted.
        self.assertEqual(counts["excluded"], 1)
        self.assertEqual(counts["included"], 1)
        self.assertEqual(published["files"][0]["path"], "app.py")


class TestPreWriteControlOwnershipGuard(unittest.TestCase):
    """Milestone F: the collision is refused BEFORE any archive byte exists.

    A guard that cannot fail is not a guard, so this test forces the collision
    directly: it wraps the inventory builder and flips the superseded control
    back to included, which is exactly what a future inventory mode would do by
    accident. That is the scenario the post-write duplicate check exists to
    catch -- but the post-write check runs only after a staging ZIP has already
    been filled with the first copy, so the operator pays for a full write
    before hearing why. The pre-write guard refuses at zero cost.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.output = self.root / "out"
        self.output.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_guard_refuses_a_payload_control_collision_before_writing(self):
        repo = self.root / "guarded"
        repo.mkdir(parents=True)
        _git(repo, "init", "-q")
        (repo / "app.py").write_bytes(b"print('x')")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "w")

        import audapack.packing as packing_mod

        real_builder = packing_mod.build_pack_inventory

        def poisoned(*args, **kwargs):
            inventory, plan = real_builder(*args, **kwargs)
            # There is no such source file here at all, which is the point: the
            # guard must refuse on the INVENTORY alone, before the writer stats
            # or reads a single byte of it.
            inventory.entries[INVENTORY_MANIFEST_PATH] = SourceInventoryEntry(
                rel=INVENTORY_MANIFEST_PATH,
                origin=ORIGIN_UNTRACKED,
                size=0,
                include=True,
                priority=1,
            )
            return inventory, plan

        packing_mod.build_pack_inventory = poisoned
        try:
            result = pack_single(
                source_path=repo,
                output_dir=self.output,
                archive_stem="Guarded",
                excludes=set(),
                delete_old=True,
                include_timestamp=False,
                manifest_meta={"project_name": "Guarded"},
            )
        finally:
            packing_mod.build_pack_inventory = real_builder

        self.assertFalse(result.success, "the guard must be reachable, not decorative")
        self.assertEqual(result.status, PACK_STATUS_FAILED_VERIFY)
        self.assertIn("reached the payload as source material", result.error_message)
        self.assertIn(INVENTORY_MANIFEST_PATH, result.error_message)
        self.assertIsNone(result.output_path)
        # Nothing was published, and no staging artifact was left behind.
        self.assertEqual(list(self.output.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
