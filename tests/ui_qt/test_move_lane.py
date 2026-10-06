"""CORE-004 regressions: Project Room move persistence is ONE serialized lane.

Rapid drops used to submit unique TaskRunner keys (registry:move:{gen}) that
ran concurrently; worker completion order -- not gesture order -- decided the
registry. The lane drains intents FIFO from one worker; newer gestures always
win. Optimistic UI response is preserved.
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def _wait_until(predicate, timeout_s=6.0):
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if app:
            app.processEvents()
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


class _MoveLaneTest(unittest.TestCase):
    def setUp(self):
        from PySide6.QtWidgets import QApplication

        self.app = QApplication.instance() or QApplication([])
        self._tmp = tempfile.mkdtemp()
        from audapack.config import AppConfig
        from audapack.services.project_service import ProjectService
        from audapack.ui_qt.main_window import MainWindow

        self.svc = ProjectService(AppConfig(), base_dir=Path(self._tmp))
        self.win = MainWindow(self.svc)
        self.win.show()

        self.projects = []
        for i in range(1, 4):
            d = Path(self._tmp) / f"App{i}"
            d.mkdir(parents=True, exist_ok=True)
            p = self.win._add_project_from_path(str(d))
            self.projects.append(p)

    def tearDown(self):
        try:
            self.win.close()
        except Exception:
            pass

    def _drop(self, src_project, target_group, target_slot, intercept=None):
        """Real drop; ``intercept`` wraps service.move_project for timing control."""
        from PySide6.QtCore import QPointF, Qt
        from PySide6.QtGui import QDropEvent

        if intercept is not None:
            real_move = self.svc.move_project

            def _wrapped(project_id, group, slot):
                return intercept(project_id, group, slot, lambda: real_move(project_id, group, slot))

            patcher = patch.object(self.svc, "move_project", side_effect=_wrapped)
            patcher.start()
            self.addCleanup(patcher.stop)

        src_idx = self.win.model.index_for_project_id(src_project.id)
        mime = self.win.model.mimeData([src_idx])
        tgt_idx = self.win.model.index_for_slot(target_group, target_slot)
        self.assertTrue(tgt_idx.isValid())
        rect = self.win.tree.visualRect(tgt_idx)
        point = QPointF(rect.center().x(), rect.center().y())
        event = QDropEvent(
            point,
            Qt.DropAction.MoveAction,
            mime,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        self.win.tree.dropEvent(event)


class TestMoveLaneOrdering(_MoveLaneTest):
    def test_a_two_rapid_drops_latest_gesture_wins(self):
        """First drop's persistence is deliberately delayed until the second
        intent is queued; the SECOND (latest) gesture must decide the final
        registry arrangement."""
        p1, p2 = self.projects[0], self.projects[1]
        gate = threading.Event()
        first_started = threading.Event()

        def _intercept(project_id, group, slot, real):
            if project_id == p1.id and not first_started.is_set():
                first_started.set()
                gate.wait(timeout=5)  # first worker deliberately stuck
                return real()
            return real()

        self._drop(p1, "MAIN0", 2, intercept=_intercept)
        self._drop(p2, "MAIN0", 1, intercept=_intercept)
        # Newer gesture submitted while first persistence is blocked.
        gate.set()
        ok = _wait_until(
            lambda: self.svc.get_project(p2.id).slot == 1
            and self.svc.get_project(p1.id).slot == 2
        )
        self.assertTrue(ok, "latest gesture did not determine final registry")
        _wait_until(lambda: not self.win.task_runner.is_running("registry:move"))

    def test_b_swap_then_newer_move_wins(self):
        p1, p2 = self.projects[0], self.projects[1]
        self._drop(p1, "MAIN0", 2)  # swap p1 <-> p2
        self._drop(p1, "MAIN0", 1)  # newer gesture: p1 back to 1
        ok = _wait_until(
            lambda: self.svc.get_project(p1.id).slot == 1
            and self.svc.get_project(p2.id).slot == 2
        )
        self.assertTrue(ok)

    def test_c_cross_group_then_newer_move(self):
        p1 = self.projects[0]
        self._drop(p1, "SIDE0", 1)
        self._drop(p1, "MAIN0", 3)  # newer gesture pulls it back
        ok = _wait_until(lambda: self.svc.get_project(p1.id).priority_group == "MAIN0" and self.svc.get_project(p1.id).slot == 3)
        self.assertTrue(ok)

    def test_d_three_rapid_gestures(self):
        p1 = self.projects[0]
        self._drop(p1, "MAIN0", 2)
        self._drop(p1, "MAIN0", 3)
        self._drop(p1, "MAIN0", 4)
        ok = _wait_until(lambda: self.svc.get_project(p1.id).slot == 4)
        self.assertTrue(ok)

    def test_e_first_persistence_failure_then_later_gesture(self):
        p1 = self.projects[0]
        calls = {"n": 0}

        def _intercept(project_id, group, slot, real):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("simulated registry write failure")
            return real()

        self._drop(p1, "MAIN0", 2, intercept=_intercept)
        self._drop(p1, "MAIN0", 3)
        ok = _wait_until(lambda: self.svc.get_project(p1.id).slot == 3)
        self.assertTrue(ok, "a later gesture should persist despite the first failure")

    def test_f_second_persistence_failure_reconciles(self):
        p1 = self.projects[0]

        def _intercept(project_id, group, slot, real):
            raise OSError("simulated registry write failure")

        self._drop(p1, "MAIN0", 2, intercept=_intercept)
        # Model reconciled from the authoritative registry: p1 stays at slot 1.
        ok = _wait_until(lambda: self.win.model.project_at("MAIN0", 1) is not None
                         and self.win.model.project_at("MAIN0", 1).id == p1.id
                         and self.svc.get_project(p1.id).slot == 1)
        self.assertTrue(ok)
        _wait_until(lambda: not self.win.task_runner.is_running("registry:move"))

    def test_g_model_matches_registry_after_drain(self):
        p1, p2, p3 = self.projects
        self._drop(p1, "MAIN0", 3)
        self._drop(p2, "MAIN0", 1)
        self._drop(p3, "SIDE0", 1)
        _wait_until(lambda: not self.win.task_runner.is_running("registry:move"))
        for project in self.projects:
            fresh = self.svc.get_project(project.id)
            self.assertEqual(
                self.win.model.project_at(fresh.priority_group, fresh.slot).id,
                project.id,
            )

    def test_i_restart_registry_equals_visible_final_state(self):
        """Persisted arrangement == visible arrangement after reconstructing
        the service from disk (CORE-004 E5)."""
        from audapack.config import load_config
        from audapack.services.project_service import ProjectService

        p1, p2, p3 = self.projects
        self._drop(p1, "MAIN0", 3)
        self._drop(p3, "SIDE0", 1)
        _wait_until(lambda: not self.win.task_runner.is_running("registry:move"))

        fresh_service = ProjectService(load_config(self.win._service.base_dir), base_dir=self.win._service.base_dir)
        for project in self.projects:
            persisted = fresh_service.get_project(project.id)
            visible = self.win.model.project_at(persisted.priority_group, persisted.slot)
            self.assertIsNotNone(visible)
            self.assertEqual(visible.id, persisted.id)

    def test_h_optimistic_ui_is_immediate(self):
        p1 = self.projects[0]
        gate = threading.Event()
        started = threading.Event()

        def _intercept(project_id, group, slot, real):
            started.set()
            gate.wait(timeout=5)
            return real()

        self._drop(p1, "MAIN0", 2, intercept=_intercept)
        # Optimistic mutation is visible BEFORE persistence completes.
        self.assertEqual(self.win.model.project_at("MAIN0", 2).id, p1.id)
        gate.set()
        _wait_until(lambda: not self.win.task_runner.is_running("registry:move"))


if __name__ == "__main__":
    unittest.main()
