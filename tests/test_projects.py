"""Unit tests for AUDAPACK Project Registry."""

import shutil
import tempfile
import unittest
from pathlib import Path

from audapack.config import AppConfig, load_config, migrate_legacy_data, save_config
from audapack.models import CANONICAL_GROUPS, SLOTS_PER_GROUP, Project
from audapack.projects import ProjectRegistry


class TestProjectRegistry(unittest.TestCase):
    def setUp(self):
        self.config = AppConfig()
        self.config.projects = []
        self.registry = ProjectRegistry(self.config)

    def test_grid_dimensions(self):
        slot_map = self.registry.get_slot_map()
        self.assertEqual(len(slot_map), 24)
        for g in CANONICAL_GROUPS:
            for s in range(1, SLOTS_PER_GROUP + 1):
                self.assertIn((g, s), slot_map)
                self.assertIsNone(slot_map[(g, s)])

    def test_add_and_get_project(self):
        p = self.registry.add_project("FastPrompter", r"C:\Code\FastPrompter", priority_group="MAIN0", slot=1)
        self.assertEqual(p.id, "fastprompter")
        self.assertEqual(p.priority_group, "MAIN0")
        self.assertEqual(p.slot, 1)

        slot_map = self.registry.get_slot_map()
        self.assertEqual(slot_map[("MAIN0", 1)], p)
        self.assertIsNone(slot_map[("MAIN0", 2)])

    def test_move_project_and_swap(self):
        p1 = self.registry.add_project("Proj1", r"C:\P1", priority_group="MAIN0", slot=1)
        p2 = self.registry.add_project("Proj2", r"C:\P2", priority_group="MAIN0", slot=2)

        # Move p1 into p2's slot -> should swap
        ok = self.registry.move_project(p1.id, "MAIN0", 2)
        self.assertTrue(ok)
        self.assertEqual(p1.slot, 2)
        self.assertEqual(p2.slot, 1)

    def test_move_project_step(self):
        p1 = self.registry.add_project("Proj1", r"C:\P1", priority_group="MAIN0", slot=1)
        # Move step +1 (down)
        self.assertTrue(self.registry.move_project_step(p1.id, 1))
        self.assertEqual(p1.priority_group, "MAIN0")
        self.assertEqual(p1.slot, 2)

        # Move step -1 (up)
        self.assertTrue(self.registry.move_project_step(p1.id, -1))
        self.assertEqual(p1.slot, 1)

        # Boundary: cannot move up beyond top slot
        self.assertFalse(self.registry.move_project_step(p1.id, -1))

    def test_remove_project_frees_slot(self):
        p = self.registry.add_project("Proj1", r"C:\P1", priority_group="MAIN0", slot=1)
        self.assertEqual(self.registry.get_project_in_slot("MAIN0", 1), p)

        self.registry.remove_project(p.id)
        self.assertIsNone(self.registry.get_project_in_slot("MAIN0", 1))

    def test_sync_from_audit_root(self):
        temp_dir = tempfile.mkdtemp()
        try:
            audit_root = Path(temp_dir)
            # Create MAIN0 and MAIN1 dirs
            (audit_root / "MAIN0" / "FastPrompter").mkdir(parents=True)
            (audit_root / "MAIN0" / "SAIPEN").mkdir(parents=True)
            (audit_root / "MAIN1" / "SAIPLAN").mkdir(parents=True)

            count = self.registry.sync_from_audit_root(audit_root)
            self.assertEqual(count, 3)

            fp = self.registry.get_project_by_name("FastPrompter")
            self.assertIsNotNone(fp)
            self.assertEqual(fp.priority_group, "MAIN0")

            sp = self.registry.get_project_by_name("SAIPLAN")
            self.assertIsNotNone(sp)
            self.assertEqual(sp.priority_group, "MAIN1")
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_legacy_migration(self):
        legacy = {
            "repos": [
                {"name": "SAIPEN", "path": r"v:\code\saipen", "enabled": True},
                {"name": "FastPrompter", "path": r"v:\code\fastprompter", "enabled": True},
            ],
            "output_dir": r"C:\Out",
            "delete_old": True,
        }
        cfg = migrate_legacy_data(legacy)
        self.assertEqual(len(cfg.projects), 2)
        p_names = {p.display_name for p in cfg.projects}
        self.assertIn("SAIPEN", p_names)
        self.assertIn("FastPrompter", p_names)
        self.assertEqual(cfg.packing.output_dir, r"C:\Out")

    def test_get_by_name_exact_wins_over_earlier_fuzzy_match(self):
        """W2-004: exact display name must outrank a fuzzy/slug collision on an
        earlier registry entry, regardless of ordering."""
        self.config.projects = []
        self.config.projects.append(Project(
            id="foo_bar", display_name="Foo.Bar",
            source_path=r"C:\FooBar", priority_group="SIDE1", slot=1,
        ))
        self.config.projects.append(Project(
            id="foo_bar_1", display_name="Foo+Bar",
            source_path=r"C:\FooPlusBar", priority_group="SIDE1", slot=2,
        ))

        # Exact display name `Foo+Bar` must resolve to its own project, not the
        # earlier entry whose generated slug (`foo_bar`) happens to collide.
        hit = self.registry.get_project_by_name("Foo+Bar")
        self.assertIsNotNone(hit)
        self.assertEqual(hit.id, "foo_bar_1")
        self.assertEqual(hit.display_name, "Foo+Bar")

        # Same guarantee with reversed registry order.
        self.config.projects.reverse()
        hit2 = self.registry.get_project_by_name("Foo+Bar")
        self.assertEqual(hit2.id, "foo_bar_1")

    def test_get_by_name_exact_beats_slug_phase(self):
        """W2-004: exact display_name wins even when a later project's slug
        matches the query before an exact-name project appears in list order."""
        self.config.projects = []
        self.config.projects.append(Project(
            id="mailer", display_name="Mailer",
            source_path=r"C:\Mailer", priority_group="SIDE1", slot=1,
        ))
        self.config.projects.append(Project(
            id="foo_bar_1", display_name="Foo+Bar",
            source_path=r"C:\FooPlusBar", priority_group="SIDE1", slot=2,
        ))
        self.config.projects.append(Project(
            id="foo_bar", display_name="Foo Bar",
            source_path=r"C:\FooBar", priority_group="SIDE1", slot=3,
        ))
        # Query is the canonical id of the third project; the id phase must hit
        # exactly it, never the second project's earlier fuzzy normalized name.
        hit = self.registry.get_project_by_name("foo_bar")
        self.assertIsNotNone(hit)
        self.assertEqual(hit.id, "foo_bar")

    def test_get_by_name_ambiguous_alias_returns_none(self):
        """W2-004: a non-exact alias matching multiple projects is ambiguous and
        must fail explicitly (None) instead of selecting by array order."""
        self.config.projects = []
        # Distinct exact names, distinct ids, but identical normalized keys.
        self.config.projects.append(Project(
            id="alpha_1", display_name="Alpha One",
            source_path=r"C:\A1", priority_group="SIDE1", slot=1,
        ))
        self.config.projects.append(Project(
            id="alpha_1_b", display_name="Alpha-One",
            source_path=r"C:\A1B", priority_group="SIDE1", slot=2,
        ))
        # "alpha_one": no exact display/audit name, no id match, normalized
        # "alphaone" matches both projects -> ambiguous, must not resolve.
        hit = self.registry.get_project_by_name("alpha_one")
        self.assertIsNone(hit, "ambiguous alias must not resolve by array order")
        # Exact display names still resolve to their own project.
        self.assertEqual(self.registry.get_project_by_name("Alpha One").id, "alpha_1")
        self.assertEqual(self.registry.get_project_by_name("Alpha-One").id, "alpha_1_b")


class TestRegistryTransactions(unittest.TestCase):
    """WJ-004: cross-process registry mutation is atomic and never falsely succeeds."""

    def setUp(self):
        self.base_dir = Path(tempfile.mkdtemp())
        save_config(AppConfig(), self.base_dir)  # seed canonical disk state

    def tearDown(self):
        shutil.rmtree(self.base_dir, ignore_errors=True)

    def _fresh_registry(self):
        from audapack.config import load_config
        return ProjectRegistry(load_config(self.base_dir), base_dir=self.base_dir, transactional=True)

    def test_concurrent_same_name_registers_exactly_one(self):
        import threading
        results = []
        errors = []
        barrier = threading.Barrier(8)

        def worker():
            reg = self._fresh_registry()
            barrier.wait()  # maximize contention
            try:
                proj, created = reg.resolve_or_register_project("BananaTool")
                results.append((proj.id, proj.slot, created))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        final = load_config(self.base_dir)
        bananas = [p for p in final.projects if p.display_name == "BananaTool"]
        self.assertEqual(len(bananas), 1)
        self.assertEqual(len({r[0] for r in results}), 1)  # one project_id everywhere
        self.assertEqual(sum(1 for r in results if r[2]), 1)  # exactly one creator

    def test_concurrent_distinct_names_get_unique_slots(self):
        import threading
        errors = []

        def worker(i):
            reg = self._fresh_registry()
            try:
                reg.resolve_or_register_project(f"Tool{i:02d}")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        final = load_config(self.base_dir)
        slots = [(p.priority_group.upper(), p.slot) for p in final.projects]
        self.assertEqual(len(slots), 12)
        self.assertEqual(len(set(slots)), 12)  # no duplicate slot
        groups = {g for g, _ in slots}
        self.assertIn("SIDE1", groups)
        self.assertIn("SIDE2", groups)  # SIDE1 full -> SIDE2 growth

    def test_config_save_retries_transient_windows_replace_failure(self):
        from unittest import mock

        original_replace = Path.replace
        attempts = 0

        def flaky_replace(source, destination):
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise PermissionError("simulated sharing violation")
            return original_replace(source, destination)

        with mock.patch.object(Path, "replace", autospec=True, side_effect=flaky_replace):
            self.assertTrue(save_config(AppConfig(), self.base_dir))
        self.assertEqual(attempts, 3)

    def test_token_read_retries_transient_windows_sharing_failure(self):
        from unittest import mock

        from audapack.config import BridgeConfig, ensure_token

        expected = "stable-token-value-for-test"
        token_file = self.base_dir / "token.txt"
        token_file.write_text(expected, encoding="utf-8")
        original_read_text = Path.read_text
        attempts = 0

        def flaky_read(path, *args, **kwargs):
            nonlocal attempts
            if path == token_file:
                attempts += 1
                if attempts < 3:
                    raise PermissionError("simulated sharing violation")
            return original_read_text(path, *args, **kwargs)

        with mock.patch.object(Path, "read_text", autospec=True, side_effect=flaky_read):
            self.assertEqual(ensure_token(BridgeConfig(), self.base_dir), expected)
        self.assertEqual(attempts, 3)

    def test_config_read_retries_transient_windows_sharing_failure(self):
        from unittest import mock

        cfg_file = self.base_dir / "config.json"
        original_read_text = Path.read_text
        attempts = 0

        def flaky_read(path, *args, **kwargs):
            nonlocal attempts
            if path == cfg_file:
                attempts += 1
                if attempts < 3:
                    raise PermissionError("simulated sharing violation")
            return original_read_text(path, *args, **kwargs)

        with mock.patch.object(Path, "read_text", autospec=True, side_effect=flaky_read):
            loaded = load_config(self.base_dir)
        self.assertIsInstance(loaded, AppConfig)
        self.assertGreaterEqual(attempts, 3)

    def test_save_failure_never_reports_success(self):
        from unittest import mock

        from audapack import projects as projects_mod

        reg = self._fresh_registry()
        with mock.patch.object(projects_mod, "save_config", return_value=False):
            with self.assertRaises(projects_mod.RegistrySaveError):
                reg.resolve_or_register_project("GhostProject")

        final = load_config(self.base_dir)
        self.assertFalse(any(p.display_name == "GhostProject" for p in final.projects))

    def test_name_aliases_resolve_to_existing_without_registration(self):
        reg1 = self._fresh_registry()
        proj, created = reg1.resolve_or_register_project("Banana Tool")
        self.assertTrue(created)

        reg2 = self._fresh_registry()
        again, created_again = reg2.resolve_or_register_project("banana_tool")
        self.assertFalse(created_again)
        self.assertEqual(proj.id, again.id)

    def test_non_transactional_mode_writes_nothing_unexpected(self):
        cfg = AppConfig()
        reg = ProjectRegistry(cfg, base_dir=self.base_dir, transactional=False)
        reg.add_project("LocalOnly", r"C:\X")
        # Legacy mode does not persist add_project; the seeded disk state is untouched.
        final = load_config(self.base_dir)
        self.assertEqual(final.projects, [])

    def test_project_ignored_flag(self):
        p = Project(
            id="test",
            display_name="Test",
            source_path=r"C:\Test",
            ignored=True,
            inaudit_aliases=["TEST TOOL", "TST"],
        )
        d = p.to_dict()
        self.assertTrue(d["ignored"])

        p2 = Project.from_dict(d)
        self.assertTrue(p2.ignored)
        self.assertEqual(p2.inaudit_aliases, ["TEST TOOL", "TST"])

        # Default is False
        p3 = Project.from_dict({"id": "test3", "display_name": "Test 3", "source_path": r"C:\Test3"})
        self.assertFalse(p3.ignored)

    def test_clear_project_marks_is_atomic_and_preserves_enablement(self):
        reg = self._fresh_registry()
        first = reg.add_project("Marked One", r"C:\One", enabled=False)
        second = reg.add_project("Marked Two", r"C:\Two", enabled=True)
        reg.edit_project(
            first.id,
            lambda p: (
                setattr(p, "ignored", True),
                setattr(p, "ignore_archive", True),
                setattr(p, "audit_copy_count", 4),
            ),
        )
        reg.edit_project(second.id, lambda p: setattr(p, "ignored", True))

        self.assertEqual(reg.clear_project_marks(), 2)
        fresh = self._fresh_registry()
        first_after = fresh.get_project_by_id(first.id)
        second_after = fresh.get_project_by_id(second.id)
        self.assertFalse(first_after.enabled)
        self.assertFalse(first_after.ignored)
        self.assertFalse(first_after.ignore_archive)
        self.assertEqual(first_after.audit_copy_count, 0)
        self.assertTrue(second_after.enabled)
        self.assertFalse(second_after.ignored)
        self.assertEqual(fresh.clear_project_marks(), 0)


class TestSlotHealing(unittest.TestCase):
    def test_out_of_range_slots_healed(self):
        cfg = AppConfig()
        cfg.projects = [
            Project(id="a", display_name="A", source_path=r"C:\A", priority_group="MAIN0", slot=1),
            Project(id="b", display_name="B", source_path=r"C:\B", priority_group="MAIN0", slot=2),
            Project(id="c", display_name="C", source_path=r"C:\C", priority_group="MAIN0", slot=3),
            Project(id="d", display_name="D", source_path=r"C:\D", priority_group="MAIN0", slot=4),
            Project(id="e", display_name="E", source_path=r"C:\E", priority_group="MAIN0", slot=5),
            Project(id="f", display_name="F", source_path=r"C:\F", priority_group="MAIN0", slot=6),
            Project(id="ghost7", display_name="Ghost7", source_path=r"C:\Ghost7", priority_group="MAIN0", slot=7),
            Project(id="ghost8", display_name="Ghost8", source_path=r"C:\Ghost8", priority_group="MAIN0", slot=8),
        ]
        changed = cfg.heal_project_slots()
        self.assertTrue(changed)
        for p in cfg.projects:
            self.assertTrue(1 <= p.slot <= SLOTS_PER_GROUP, f"{p.display_name} still at slot {p.slot}")
        occupied = [(p.priority_group.upper(), p.slot) for p in cfg.projects]
        self.assertEqual(len(occupied), len(set(occupied)), "slots must be unique per group")
        # Original 6 stay in MAIN0; overflow must land in a later group, never MAIN0 #7.
        main0 = sorted(p.slot for p in cfg.projects if p.priority_group.upper() == "MAIN0")
        self.assertEqual(main0, [1, 2, 3, 4, 5, 6])

    def test_valid_slots_not_touched(self):
        cfg = AppConfig()
        cfg.projects = [
            Project(id="a", display_name="A", source_path=r"C:\A", priority_group="MAIN0", slot=1),
            Project(id="b", display_name="B", source_path=r"C:\B", priority_group="MAIN0", slot=2),
        ]
        self.assertFalse(cfg.heal_project_slots())
        self.assertEqual(
            [(p.display_name, p.slot) for p in cfg.projects],
            [("A", 1), ("B", 2)],
        )

    def test_colliding_slots_healed(self):
        cfg = AppConfig()
        cfg.projects = [
            Project(id="a", display_name="A", source_path=r"C:\A", priority_group="MAIN0", slot=1),
            Project(id="b", display_name="B", source_path=r"C:\B", priority_group="MAIN0", slot=1),
        ]
        changed = cfg.heal_project_slots()
        self.assertTrue(changed)
        slots = sorted(p.slot for p in cfg.projects)
        self.assertEqual(slots, [1, 2])


if __name__ == "__main__":
    unittest.main()
