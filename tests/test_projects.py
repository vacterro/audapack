"""Unit tests for AUDAPACK Project Registry."""

import shutil
import tempfile
import unittest
from pathlib import Path

from audapack.config import AppConfig, create_default_projects, load_config, migrate_legacy_data, save_config
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
        p = Project(id="test", display_name="Test", source_path=r"C:\Test", ignored=True)
        d = p.to_dict()
        self.assertTrue(d["ignored"])

        p2 = Project.from_dict(d)
        self.assertTrue(p2.ignored)

        # Default is False
        p3 = Project.from_dict({"id": "test3", "display_name": "Test 3", "source_path": r"C:\Test3"})
        self.assertFalse(p3.ignored)


if __name__ == "__main__":
    unittest.main()
