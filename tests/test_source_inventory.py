"""T-190 (SRC-046) focused tests: Git-aware fail-closed source inventory.

Covers every contract clause of the frozen inventory stage:

- Git inventory = tracked_existing UNION untracked_nonignored.
- tracked project truth overrides ordinary noise exclusions.
- hard-safety conflicts fail closed.
- tracked deletions are explicit (``tracked_deleted``), absences Git cannot
  explain fail the pack.
- no per-file Git subprocesses (bounded command count).
- no recursive traversal of ignored recovery/cache trees.
- single-file packing remains supported.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from audapack.config import PackingConfig
from audapack.packing import SourceChangedError, stage_inventory_zip
from audapack.source_inventory import (
    CODE_INVENTORY_INCONSISTENT,
    CODE_SUBMODULE_REQUIRES_EXPLICIT_POLICY,
    CODE_TRACKED_HARD_DENY_CONFLICT,
    CODE_TRACKED_RESERVED_ARCHIVE_NAME_CONFLICT,
    HARD_SAFETY_EXCLUDES,
    REASON_TRACKED_DELETED,
    REASON_UNTRACKED_NON_REGULAR,
    RESERVED_ARCHIVE_NAMES,
    SourceInventoryError,
    build_git_inventory,
    build_pack_inventory,
    detect_git_worktree,
    inventory_from_single_file,
    reserved_archive_conflict_message,
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "commit.gpgsign=false", *args],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )


def _init_git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    return path


class TestGitInventoryContract(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.repo = _init_git_repo(self.root / "proj")

    def tearDown(self):
        self._tmp.cleanup()

    def test_inventory_is_tracked_union_untracked_nonignored(self):
        (self.repo / "app.py").write_text("print('x')", encoding="utf-8")
        (self.repo / "media.wav").write_bytes(b"0" * 16)
        (self.repo / "new_source.py").write_text("NEW", encoding="utf-8")
        (self.repo / ".gitignore").write_text("ignored.tmp\n_recovery/\n", encoding="utf-8")
        (self.repo / "ignored.tmp").write_text("noise", encoding="utf-8")
        recovery = self.repo / "_recovery"
        recovery.mkdir()
        (recovery / "junk.bin").write_bytes(b"x" * 8)
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "init")

        inv, plan = build_pack_inventory(self.repo, set())
        self.assertEqual(inv.mode, "git")
        self.assertIsNone(plan)
        included = {e.rel for e in inv.included_entries()}
        self.assertEqual(included, {"app.py", "media.wav", "new_source.py", ".gitignore"})
        self.assertNotIn("ignored.tmp", included)
        self.assertFalse(any(rel.startswith("_recovery/") for rel in inv.entries))

    def test_tracked_truth_overrides_ordinary_noise_excludes(self):
        tracked = self.repo / "media.wav"
        tracked.write_bytes(b"AUDIO" * 4)
        (self.repo / ".gitignore").write_text("*.log\n", encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "init")
        # Also a tracked file matching a "recovery-looking" noise name.
        (self.repo / "bak_old_thing.txt").write_text("real source", encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "more")

        inv, _ = build_pack_inventory(self.repo, excludes={"*.wav", "*bak*", "*.log"})
        included = {e.rel for e in inv.included_entries()}
        self.assertIn("media.wav", included)
        self.assertIn("bak_old_thing.txt", included)

    def test_hard_safety_conflict_fails_closed(self):
        secret = self.repo / "token.txt"
        secret.write_text("SECRET", encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "oops")
        with self.assertRaises(SourceInventoryError) as ctx:
            build_git_inventory(self.repo, set())
        self.assertEqual(ctx.exception.code, CODE_TRACKED_HARD_DENY_CONFLICT)

    def test_tracked_private_key_keeps_hard_safety_classification(self):
        """A credential refusal is not a naming collision and must not be
        re-labelled: the reserved-name split must leave it alone."""
        key = self.repo / "id_rsa"
        key.write_text("PRIVATE", encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "oops")
        with self.assertRaises(SourceInventoryError) as ctx:
            build_git_inventory(self.repo, set())
        self.assertEqual(ctx.exception.code, CODE_TRACKED_HARD_DENY_CONFLICT)
        self.assertIn("hard-safety deny policy", ctx.exception.message)
        self.assertNotIn("reserved", ctx.exception.message.lower())

    def _tracked_reserved_repo(self, rel: str) -> None:
        path = self.repo.joinpath(*rel.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "init")

    def test_reserved_archive_name_conflicts_are_their_own_classification(self):
        """A tracked reserved archive-control path is a COLLISION, not a secret.

        The old diagnostic reported it as TRACKED_HARD_DENY_CONFLICT/"hard-safety
        deny policy", which sent the operator hunting for a credential instead
        of fixing a name. Both reserved names must name the real rule, the real
        path, and the real repair -- and must still fail closed.
        """
        for rel in sorted(RESERVED_ARCHIVE_NAMES):
            with self.subTest(rel=rel):
                with tempfile.TemporaryDirectory() as tmp:
                    self.repo = _init_git_repo(Path(tmp) / "proj")
                    self._tracked_reserved_repo(rel)
                    with self.assertRaises(SourceInventoryError) as ctx:
                        build_git_inventory(self.repo, set())
                    exc = ctx.exception
                    self.assertEqual(exc.code, CODE_TRACKED_RESERVED_ARCHIVE_NAME_CONFLICT)
                    self.assertEqual(exc.rel, rel)
                    self.assertEqual(exc.message, reserved_archive_conflict_message(rel))
                    self.assertIn(f"'{rel}'", exc.message)
                    self.assertIn(
                        "Remove it from source control or rename the project-owned "
                        "file before packing.",
                        exc.message,
                    )
                    # The machine classification carries both tokens, in order.
                    self.assertIn(
                        f"[{CODE_TRACKED_RESERVED_ARCHIVE_NAME_CONFLICT}]", str(exc)
                    )

    def test_deleted_tracked_reserved_path_still_conflicts(self):
        """Deleting the worktree copy does not launder a reserved-name
        conflict: the path is still tracked, so it still fails closed."""
        self._tracked_reserved_repo("_AUDAPACK_MANIFEST.json")
        self.repo.joinpath("_AUDAPACK_MANIFEST.json").unlink()
        with self.assertRaises(SourceInventoryError) as ctx:
            build_git_inventory(self.repo, set())
        self.assertEqual(ctx.exception.code, CODE_TRACKED_RESERVED_ARCHIVE_NAME_CONFLICT)
        self.assertEqual(ctx.exception.rel, "_AUDAPACK_MANIFEST.json")

    def test_tracked_deletion_is_explicit_not_silent(self):
        victim = self.repo / "gone.py"
        victim.write_text("x", encoding="utf-8")
        keep = self.repo / "app.py"
        keep.write_text("print('x')", encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "init")
        self.repo.joinpath("gone.py").unlink()  # worktree deletion, staged? no: unstaged " D"

        inv, _ = build_pack_inventory(self.repo, set())
        self.assertIn("gone.py", inv.tracked_deleted)
        excluded = {e.rel: e for e in inv.excluded_entries()}
        self.assertEqual(excluded["gone.py"].reason, REASON_TRACKED_DELETED)
        self.assertNotIn("gone.py", {e.rel for e in inv.included_entries()})
        self.assertIn("app.py", {e.rel for e in inv.included_entries()})

    def test_unexplained_tracked_absence_fails_closed(self):
        victim = self.repo / "ghost.py"
        victim.write_text("x", encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "init")
        self.repo.joinpath("ghost.py").unlink()
        # Remove the deletion from git's view by rewriting the index entry:
        # simulate a stat/index lie with a stale index (update without refresh).
        _git(self.repo, "update-index", "--assume-unchanged", "ghost.py")
        with self.assertRaises(SourceInventoryError) as ctx:
            build_git_inventory(self.repo, set())
        self.assertEqual(ctx.exception.code, CODE_INVENTORY_INCONSISTENT)

    def test_submodule_gitlink_fails_closed(self):
        # protocol.file.allow is disabled by default in modern git; a local
        # submodule add may therefore fail on some installs. Rather than
        # depending on that, forge the gitlink directly: write the index
        # entry with update-index so the inventory sees mode 160000.
        nested = self.root / "nested"
        nested.mkdir()
        (nested / "inner.txt").write_text("i", encoding="utf-8")
        _init_git_repo(nested)
        _git(nested, "add", "-A")
        _git(nested, "commit", "-q", "-m", "inner")
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(nested), check=True, capture_output=True, text=True
        ).stdout.strip()
        (self.repo / "app.py").write_text("print('x')", encoding="utf-8")
        vendor = self.repo / "vendor" / "nested"
        vendor.mkdir(parents=True)
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "init")
        _git(self.repo, "update-index", "--add", "--cacheinfo", f"160000,{head},vendor/nested")
        _git(self.repo, "commit", "-q", "-m", "submodule")
        with self.assertRaises(SourceInventoryError) as ctx:
            build_git_inventory(self.repo, set())
        self.assertEqual(ctx.exception.code, CODE_SUBMODULE_REQUIRES_EXPLICIT_POLICY)

    def test_untracked_nested_git_worktree_is_named_omission_not_crash(self):
        """Git reports a nested-clone directory as "dir/" with a trailing slash.

        Refusing it as PATH_INVALID failed the whole pack on the real SAIPEN
        tree. Git cannot enumerate inside another worktree, so the directory
        becomes an explicit excluded entry with reason untracked_git_directory
        -- the same philosophy as tracked_deleted: named, never silent.
        """
        (self.repo / "app.py").write_text("print('x')", encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "init")
        nested = self.repo / "vendor-sandbox"
        nested.mkdir()
        _git(nested, "init", "-q")
        (nested / "inner.txt").write_text("nested", encoding="utf-8")

        inv = build_git_inventory(self.repo, set())
        self.assertIn("app.py", {e.rel for e in inv.included_entries()})
        excluded = {e.rel: e for e in inv.excluded_entries()}
        self.assertIn("vendor-sandbox", excluded)
        self.assertEqual(excluded["vendor-sandbox"].reason, "untracked_git_directory")
        self.assertFalse(excluded["vendor-sandbox"].include)
        self.assertNotIn("vendor-sandbox", {e.rel for e in inv.included_entries()})

    def test_untracked_non_regular_is_named_omission_not_pack_failure(self):
        """An untracked FIFO/socket/device must not fail the WHOLE pack.

        This is the exact defect the user's screenshot caught: AUDAPACK
        reported "PACK FAILED (Wintage): [FAILED_INVENTORY] untracked path is
        not a regular file" because ONE transient special file in the worktree
        raised SourceInventoryError. Git never promised an untracked special
        file was project truth, so it is recorded as an explicit excluded entry
        (reason untracked_non_regular) -- named, never silent, never a whole-pack
        crash. A TRACKED non-regular still fails closed (covered elsewhere).

        Platform-independent: os.mkfifo is Unix-only, so the non-regular st_mode
        is injected through lstat for the one special path while every other
        path stats normally.
        """
        import stat as _stat
        (self.repo / "app.py").write_text("print('x')", encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "init")
        # An untracked, non-ignored path Git will list via ls-files --others.
        special = self.repo / "runtime.sock"
        special.write_text("", encoding="utf-8")

        import audapack.source_inventory as si
        real_lstat = os.lstat

        def fake_lstat(path, *a, **kw):
            st = real_lstat(path, *a, **kw)
            if str(path).replace("\\", "/").endswith("/runtime.sock"):
                # S_IFIFO: a named pipe -- neither regular, symlink, nor dir.
                new_mode = (st.st_mode & ~0o170000) | _stat.S_IFIFO
                return os.stat_result(
                    (new_mode,) + tuple(st)[1:]
                )
            return st

        with unittest.mock.patch.object(si.os, "lstat", side_effect=fake_lstat):
            inv = build_git_inventory(self.repo, set())

        self.assertIn("app.py", {e.rel for e in inv.included_entries()})
        excluded = {e.rel: e for e in inv.excluded_entries()}
        self.assertIn("runtime.sock", excluded)
        self.assertEqual(excluded["runtime.sock"].reason, REASON_UNTRACKED_NON_REGULAR)
        self.assertFalse(excluded["runtime.sock"].include)
        self.assertNotIn("runtime.sock", {e.rel for e in inv.included_entries()})

    def test_tracked_non_regular_still_fails_closed(self):
        """The downgrade is UNTRACKED-only: a TRACKED non-regular still fails.

        The index promised content for a tracked path; a non-regular object
        there is a genuine inconsistency, not a transient worktree artifact, so
        it must remain a hard SourceInventoryError (asymmetry is the point).
        """
        import stat as _stat
        (self.repo / "tracked.bin").write_text("data", encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "init")

        import audapack.source_inventory as si
        real_lstat = os.lstat

        def fake_lstat(path, *a, **kw):
            st = real_lstat(path, *a, **kw)
            if str(path).replace("\\", "/").endswith("/tracked.bin"):
                new_mode = (st.st_mode & ~0o170000) | _stat.S_IFIFO
                return os.stat_result((new_mode,) + tuple(st)[1:])
            return st

        with unittest.mock.patch.object(si.os, "lstat", side_effect=fake_lstat):
            with self.assertRaises(SourceInventoryError) as ctx:
                build_git_inventory(self.repo, set())
        self.assertEqual(ctx.exception.code, CODE_INVENTORY_INCONSISTENT)
        self.assertIn("tracked path is not a regular file", ctx.exception.message)

    def test_bounded_git_command_count(self):
        for i in range(60):
            p = self.repo / f"f{i}.py"
            p.write_text(f"x={i}", encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "many")

        calls = {"n": 0}
        real_run = subprocess.run

        def counting_run(argv, *a, **kw):
            if argv and argv[0] == "git":
                calls["n"] += 1
            return real_run(argv, *a, **kw)

        import audapack.source_inventory as si

        orig = si.subprocess.run
        si.subprocess.run = counting_run
        try:
            build_git_inventory(self.repo, set())
        finally:
            si.subprocess.run = orig
        # Four content commands + HEAD + (detect) -- never O(files).
        self.assertLessEqual(calls["n"], 8)

    def test_ignored_recovery_tree_never_enumerated(self):
        (self.repo / ".gitignore").write_text("_RECOVERY_T1227/\n", encoding="utf-8")
        (self.repo / "app.py").write_text("print('x')", encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "init")
        recovery = self.repo / "_RECOVERY_T1227" / "deep" / "deeper"
        recovery.mkdir(parents=True)
        for i in range(20):
            (recovery / f"junk{i}.bin").write_bytes(b"x" * 8)

        inv, _ = build_pack_inventory(self.repo, set())
        self.assertFalse(any(rel.startswith("_RECOVERY_T1227/") for rel in inv.entries))


class TestProtectedControlPlaneOverlay(unittest.TestCase):
    """T-188 P0: gitignore must not erase protected priority-1 control plane.

    The T-190 Git inventory is tracked UNION untracked_nonignored, but this
    repository's .gitignore intentionally hides .saipen/intake/ and audit/.
    The overlay re-enumerates ONLY the semantic protected locations (never a
    generic ignored-tree walk) and feeds every candidate through the same
    validation and policy cascade as untracked material.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.repo = _init_git_repo(self.root / "proj")

    def tearDown(self):
        self._tmp.cleanup()

    def _commit_base(self, gitignore: str):
        (self.repo / "app.py").write_text("print('x')", encoding="utf-8")
        (self.repo / ".gitignore").write_text(gitignore, encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "init")

    def test_ignored_intake_and_audit_material_is_inventoried(self):
        self._commit_base(".saipen/intake/\naudit/\n.saipen/audit/\n")
        intake = self.repo / ".saipen" / "intake" / "active"
        intake.mkdir(parents=True)
        (intake / "SRC-044.md").write_text("SRC-044 RECEIPT", encoding="utf-8")
        (intake / "SRC-044.meta.json").write_text("{}", encoding="utf-8")
        audit = self.repo / "audit"
        audit.mkdir()
        (audit / "10.md").write_text("AUDIT TEN", encoding="utf-8")
        saipen_audit = self.repo / ".saipen" / "audit"
        saipen_audit.mkdir(parents=True)
        (saipen_audit / "7.md").write_text("LAYER 7", encoding="utf-8")

        inv = build_git_inventory(self.repo, set())
        included = {e.rel for e in inv.included_entries()}
        self.assertIn(".saipen/intake/active/SRC-044.md", included)
        self.assertIn(".saipen/intake/active/SRC-044.meta.json", included)
        self.assertIn("audit/10.md", included)
        self.assertIn(".saipen/audit/7.md", included)
        by_rel = {e.rel: e for e in inv.entries.values()}
        for rel in (
            ".saipen/intake/active/SRC-044.md",
            "audit/10.md",
            ".saipen/audit/7.md",
        ):
            self.assertEqual(by_rel[rel].origin, "untracked", rel)
            self.assertEqual(by_rel[rel].priority, 1, rel)
            self.assertIsNone(by_rel[rel].reason, rel)

    def test_ignored_nonnumeric_audit_md_is_not_discovered(self):
        self._commit_base("audit/\n")
        (self.repo / "audit").mkdir()
        (self.repo / "audit" / "notes.md").write_text("not audit layer", encoding="utf-8")
        (self.repo / "audit" / "10.md").write_text("AUDIT TEN", encoding="utf-8")

        inv = build_git_inventory(self.repo, set())
        included = {e.rel for e in inv.included_entries()}
        self.assertIn("audit/10.md", included)
        self.assertNotIn("audit/notes.md", included)

    def test_unrelated_ignored_trees_stay_absent_beside_protected(self):
        self._commit_base(
            ".saipen/intake/\naudit/\n.saipen/recovery/\n_RECOVERY_X/\ncache/\n"
        )
        intake = self.repo / ".saipen" / "intake" / "active"
        intake.mkdir(parents=True)
        (intake / "SRC-044.md").write_text("SRC-044", encoding="utf-8")
        (self.repo / "audit").mkdir()
        (self.repo / "audit" / "10.md").write_text("AUDIT TEN", encoding="utf-8")
        for tree in (".saipen/recovery/deep/deeper", "_RECOVERY_X", "cache"):
            d = self.repo / tree
            d.mkdir(parents=True)
            for i in range(10):
                (d / f"junk{i}.bin").write_bytes(b"x" * 8)

        inv = build_git_inventory(self.repo, set())
        rels = set(inv.entries)
        for prefix in (".saipen/recovery/", "_RECOVERY_X/", "cache/"):
            self.assertFalse(
                any(rel.startswith(prefix) for rel in rels),
                f"ignored tree {prefix} must never enter inventory",
            )
        self.assertIn(".saipen/intake/active/SRC-044.md", rels)
        self.assertIn("audit/10.md", rels)

    def test_hard_safety_wins_for_protected_candidates(self):
        self._commit_base(".saipen/intake/\n")
        intake = self.repo / ".saipen" / "intake" / "active"
        intake.mkdir(parents=True)
        (intake / "token.txt").write_text("SECRET", encoding="utf-8")
        (intake / "SRC-044.secret").write_text("SECRET", encoding="utf-8")
        (intake / "SRC-044.md").write_text("SRC-044", encoding="utf-8")

        inv = build_git_inventory(self.repo, set())
        included = {e.rel for e in inv.included_entries()}
        self.assertIn(".saipen/intake/active/SRC-044.md", included)
        self.assertNotIn(".saipen/intake/active/token.txt", included)
        self.assertNotIn(".saipen/intake/active/SRC-044.secret", included)
        excluded = {e.rel: e for e in inv.excluded_entries()}
        self.assertEqual(
            excluded[".saipen/intake/active/token.txt"].reason, "secret_policy"
        )
        self.assertEqual(
            excluded[".saipen/intake/active/SRC-044.secret"].reason, "secret_policy"
        )

    def test_explicit_configured_exclude_keeps_precedence_over_protected(self):
        """Requirement 6: only GIT ignore is neutralized, never explicit policy."""
        self._commit_base("audit/\n")
        (self.repo / "audit").mkdir()
        (self.repo / "audit" / "10.md").write_text("AUDIT TEN", encoding="utf-8")

        inv = build_git_inventory(self.repo, {"audit/"})
        included = {e.rel for e in inv.included_entries()}
        self.assertNotIn("audit/10.md", included)
        excluded = {e.rel: e for e in inv.excluded_entries()}
        self.assertIsNotNone(excluded["audit/10.md"].reason)

    def test_ignored_always_include_exact_file_is_inventoried(self):
        self._commit_base("vault/\n")
        vault = self.repo / "vault"
        vault.mkdir()
        (vault / "required.md").write_text("REQUIRED", encoding="utf-8")

        inv = build_git_inventory(
            self.repo, set(), always_include=["vault/required.md"]
        )
        included = {e.rel for e in inv.included_entries()}
        self.assertIn("vault/required.md", included)
        by_rel = {e.rel: e for e in inv.entries.values()}
        self.assertEqual(by_rel["vault/required.md"].priority, 1)

    def test_ignored_manifest_required_file_is_inventoried(self):
        (self.repo / "MANIFEST.json").write_text(
            '{"required": ["vault/asset.bin"]}', encoding="utf-8"
        )
        (self.repo / "app.py").write_text("print('x')", encoding="utf-8")
        (self.repo / ".gitignore").write_text("vault/\n", encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "init")
        vault = self.repo / "vault"
        vault.mkdir()
        (vault / "asset.bin").write_bytes(b"ASSET")

        inv = build_git_inventory(self.repo, set())
        included = {e.rel for e in inv.included_entries()}
        self.assertIn("vault/asset.bin", included)
        by_rel = {e.rel: e for e in inv.entries.values()}
        self.assertEqual(by_rel["vault/asset.bin"].priority, 1)

    def test_protected_overlay_change_between_freeze_and_write_fails_closed(self):
        self._commit_base("audit/\n")
        (self.repo / "audit").mkdir()
        (self.repo / "audit" / "10.md").write_text("AUDIT TEN", encoding="utf-8")

        inv, _ = build_pack_inventory(self.repo, set())
        self.assertIn("audit/10.md", {e.rel for e in inv.included_entries()})

        # Rewrite AFTER the freeze: the frozen stat identity no longer holds.
        (self.repo / "audit" / "10.md").write_text(
            "AUDIT TEN, TAMPERED", encoding="utf-8"
        )
        out_zip = self.root / "out" / "Proj.zip"
        with self.assertRaises(SourceChangedError):
            stage_inventory_zip(self.repo, out_zip, inv)
        self.assertFalse(out_zip.exists())


class TestSingleFileInventory(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_single_file_source_has_parent_write_root(self):
        f = self.root / "notes.md"
        f.write_text("hello", encoding="utf-8")
        inv = inventory_from_single_file(f, set())
        self.assertEqual(inv.source, self.root.resolve())
        self.assertEqual([e.rel for e in inv.included_entries()], ["notes.md"])

    def test_single_file_secret_is_refused(self):
        f = self.root / "token.txt"
        f.write_text("SECRET", encoding="utf-8")
        with self.assertRaises(SourceInventoryError):
            inventory_from_single_file(f, set())


class TestFilesystemTraversalUnderChange(unittest.TestCase):
    """Non-Git sources: a live tree changes under the walk.

    Observed on a project whose own agent tool was running: session and lock
    directories were removed between the parent's listing and their read, and
    every such race failed the pack as "source traversal incomplete" without
    naming a path.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.src = Path(self._tmp.name) / "proj"
        (self.src / "runtime" / "sess_1").mkdir(parents=True)
        (self.src / "main.py").write_text("print(1)\n", encoding="utf-8")
        (self.src / "runtime" / "sess_1" / "log.txt").write_text("x", encoding="utf-8")
        self.assertIsNone(detect_git_worktree(self.src))

    def tearDown(self):
        self._tmp.cleanup()

    def _scandir_failing(self, name, exc):
        real = os.scandir

        def fake(path="."):
            if Path(path).name == name:
                raise exc
            return real(path)

        return unittest.mock.patch.object(os, "scandir", fake)

    def test_vanished_directory_does_not_fail_the_plan_inventory(self):
        with self._scandir_failing("sess_1", FileNotFoundError(2, "gone")):
            inv, plan = build_pack_inventory(self.src, set(), packing=PackingConfig())
        self.assertIn("main.py", [e.rel for e in inv.included_entries()])
        self.assertEqual(plan.vanished_entries, 1)

    def test_vanished_directory_does_not_fail_the_legacy_walk(self):
        with self._scandir_failing("sess_1", FileNotFoundError(2, "gone")):
            inv, _ = build_pack_inventory(self.src, set())
        self.assertIn("main.py", [e.rel for e in inv.included_entries()])

    def test_unreadable_directory_fails_and_names_the_path(self):
        with self._scandir_failing("sess_1", OSError(13, "denied")):
            with self.assertRaises(SourceInventoryError) as ctx:
                build_pack_inventory(self.src, set(), packing=PackingConfig())
        self.assertEqual(ctx.exception.code, CODE_INVENTORY_INCONSISTENT)
        self.assertEqual(ctx.exception.rel, "runtime/sess_1")
        self.assertIn("runtime/sess_1", str(ctx.exception))

    def test_unreadable_directory_still_fails_the_legacy_walk(self):
        with self._scandir_failing("sess_1", OSError(13, "denied")):
            with self.assertRaises(SourceInventoryError):
                build_pack_inventory(self.src, set())


class TestHardSafetyPolicy(unittest.TestCase):
    def test_hard_safety_set_is_secret_scoped(self):
        # Ordinary noise must NOT be hard safety: tracked truth wins.
        self.assertNotIn("*.log", HARD_SAFETY_EXCLUDES)
        self.assertNotIn("*.tmp", HARD_SAFETY_EXCLUDES)
        self.assertIn("token.txt", HARD_SAFETY_EXCLUDES)
        self.assertIn("*.secret", HARD_SAFETY_EXCLUDES)

    def test_reserved_names_are_archive_control_only(self):
        self.assertEqual(
            RESERVED_ARCHIVE_NAMES,
            {"_AUDAPACK_MANIFEST.json", ".audapack/manifest.json"},
        )


class TestDetectGitWorktree(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_non_git_dir_returns_none(self):
        plain = self.root / "plain"
        plain.mkdir()
        (plain / "a.txt").write_text("x", encoding="utf-8")
        self.assertIsNone(detect_git_worktree(plain))

    def test_git_dir_is_detected(self):
        repo = _init_git_repo(self.root / "repo")
        (repo / "a.txt").write_text("x", encoding="utf-8")
        self.assertIsNotNone(detect_git_worktree(repo))

    def test_child_of_git_repo_resolves_metadata(self):
        repo = _init_git_repo(self.root / "repo")
        sub = repo / "src" / "deep"
        sub.mkdir(parents=True)
        (sub / "a.txt").write_text("x", encoding="utf-8")
        self.assertIsNotNone(detect_git_worktree(sub))


if __name__ == "__main__":
    unittest.main()
