"""ProjectService — move/add/remove/resolve via targeted results."""

import pathlib
import shutil
import tempfile

from audapack.config import AppConfig, save_config
from audapack.services.project_service import ProjectService


def _isolated_service():
    base = pathlib.Path(tempfile.mkdtemp())
    cfg = AppConfig()
    cfg.projects = []
    save_config(cfg, base)
    svc = ProjectService(base_dir=base)
    return svc, base


def test_move_returns_specific_result():
    svc, base = _isolated_service()
    try:
        p1 = svc.add_project("Alpha", r"C:\Alpha", priority_group="MAIN0", slot=1)
        p2 = svc.add_project("Beta", r"C:\Beta", priority_group="MAIN0", slot=2)
        res = svc.move_project(p1.id, "MAIN0", 2)
        assert res.ok
        assert res.old_group == "MAIN0" and res.old_slot == 1
        assert res.new_group == "MAIN0" and res.new_slot == 2
        assert res.swapped_project_id == p2.id
        # second move via service, not direct registry
        res2 = svc.move_project(p2.id, "SIDE1", 1)
        assert res2.ok and res2.new_group == "SIDE1"
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_add_remove_resolve():
    svc, base = _isolated_service()
    try:
        proj = svc.add_project("Gamma", r"C:\Gamma")
        assert proj.display_name == "Gamma"
        found, created = svc.resolve_project("Gamma")
        assert not created and found.id == proj.id
        assert svc.remove_project(proj.id)
        assert svc.get_project(proj.id) is None
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_occupied_count_counts_actual_projects():
    svc, base = _isolated_service()
    try:
        # 0 projects
        assert svc.occupied_count("MAIN0") == 0
        # 1 project
        svc.add_project("A", r"C:\A", priority_group="MAIN0", slot=1)
        assert svc.occupied_count("MAIN0") == 1
        # 3 projects
        svc.add_project("B", r"C:\B", priority_group="MAIN0", slot=2)
        svc.add_project("C", r"C:\C", priority_group="MAIN0", slot=3)
        assert svc.occupied_count("MAIN0") == 3
        # 6 projects
        for i in range(4, 7):
            svc.add_project(f"D{i}", rf"C:\D{i}", priority_group="MAIN0", slot=i)
        assert svc.occupied_count("MAIN0") == 6
        # A different group stays empty
        assert svc.occupied_count("SIDE1") == 0
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_add_project_uses_single_registry_transaction():
    from unittest import mock

    from audapack import projects as projects_mod

    svc, base = _isolated_service()
    try:
        with mock.patch.object(projects_mod, "save_config", wraps=projects_mod.save_config) as m:
            ps_loaded = svc.registry
            ps_loaded.add_project("Single", r"C:\Single", priority_group="MAIN0", slot=1)
            # exactly one persistence call for one logical mutation
            assert m.call_count == 1
    finally:
        shutil.rmtree(base, ignore_errors=True)
