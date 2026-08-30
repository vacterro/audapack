"""End-to-end Drag & Drop pipeline regression (no handler mocks).

Guards against the Wave M refactor regression where ``project_dropped`` had no
connected handler, so drops updated nothing. Covers:
- drag initiation through the real Qt press->move state machine,
- drop -> signal -> optimistic model mutation -> async persistence,
- swap (two projects exchange slots),
- cross-group move via drop on a group header.
"""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def _wait_until(predicate, timeout_s=3.0):
    """Pumps the Qt event loop until predicate() is true or timeout expires."""
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


class TestDnDPipeline(unittest.TestCase):
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

        d1 = Path(self._tmp) / "App1"
        d2 = Path(self._tmp) / "App2"
        d1.mkdir(parents=True, exist_ok=True)
        d2.mkdir(parents=True, exist_ok=True)
        self.p1 = self.win._add_project_from_path(str(d1))
        self.p2 = self.win._add_project_from_path(str(d2))
        self.assertIsNotNone(self.p1)
        self.assertIsNotNone(self.p2)

    def tearDown(self):
        try:
            self.win.close()
        except Exception:
            pass

    # ------------------------------------------------------------------ helpers

    def _drop(self, src_project, target_group, target_slot, on_header=False):
        """Performs a real tree.dropEvent() with the model's own mime payload."""
        from PySide6.QtCore import QPointF, Qt
        from PySide6.QtGui import QDropEvent

        src_idx = self.win.model.index_for_project_id(src_project.id)
        mime = self.win.model.mimeData([src_idx])
        self.assertTrue(mime.hasFormat("application/x-audapack-project"))

        if on_header:
            tgt_idx = self.win.model.index(0, 0)  # group header row 0
            g_idx = self.win.model.index_for_slot(target_group, 1).parent()
            tgt_idx = g_idx
        else:
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

    # ------------------------------------------------------------------ tests

    def test_a_drag_start_fires_from_real_press_move(self):
        from PySide6.QtCore import QPoint, Qt
        from PySide6.QtTest import QTest

        from audapack.ui_qt.main_window import MainWindow as MW
        from audapack.ui_qt.main_window import ProjectTreeView

        d1 = Path(self._tmp) / "DragApp"
        d1.mkdir(parents=True, exist_ok=True)
        proj = self.svc.add_project("DragApp", source_path=str(d1))

        win = MW(self.svc)
        win.show()
        started = []
        orig = ProjectTreeView.startDrag
        ProjectTreeView.startDrag = lambda self_, actions: started.append(actions)
        try:
            idx = win.model.index_for_project_id(proj.id)
            rect = win.tree.visualRect(idx)
            center = rect.center()
            QTest.mousePress(win.tree.viewport(), Qt.MouseButton.LeftButton, pos=center)
            for dy in range(2, 40, 4):
                QTest.mouseMove(
                    win.tree.viewport(), QPoint(center.x(), center.y() - dy)
                )
            QTest.mouseRelease(
                win.tree.viewport(), Qt.MouseButton.LeftButton, pos=center
            )
        finally:
            ProjectTreeView.startDrag = orig
            try:
                win.close()
            except Exception:
                pass
        self.assertTrue(started, "drag never started on press+move")

    def test_b_swap_two_projects_via_real_drop(self):
        self.assertEqual(self.p1.slot, 1)
        self.assertEqual(self.p2.slot, 2)

        # Drop p1 (slot 1) onto p2's row (slot 2) -> swap
        self._drop(self.p1, "MAIN0", 2)

        # Model must reflect the swap immediately (optimistic update)
        self.assertEqual(self.win.model.project_at("MAIN0", 1).id, self.p2.id)
        self.assertEqual(self.win.model.project_at("MAIN0", 2).id, self.p1.id)

        # Registry must persist the swap (async -> pump loop until visible)
        ok = _wait_until(
            lambda: self.svc.get_project(self.p1.id).slot == 2
            and self.svc.get_project(self.p2.id).slot == 1
        )
        self.assertTrue(ok, "swap was not persisted to the registry")

    def test_c_cross_group_move_via_group_header_drop(self):
        # Drop p1 onto SIDE0 group header -> first free slot SIDE0 #1
        self._drop(self.p1, "SIDE0", 1, on_header=True)

        ok = _wait_until(
            lambda: self.svc.get_project(self.p1.id).priority_group == "SIDE0"
            and self.svc.get_project(self.p1.id).slot == 1
        )
        self.assertTrue(ok, "cross-group move was not persisted")
        self.assertEqual(self.win.model.project_at("SIDE0", 1).id, self.p1.id)
        # Move into an EMPTY slot: MAIN0#1 becomes empty, p2 stays at MAIN0#2
        self.assertIsNone(self.win.model.project_at("MAIN0", 1))
        self.assertEqual(self.win.model.project_at("MAIN0", 2).id, self.p2.id)

    def test_drag_start_fires_from_real_press_move(self):
        from PySide6.QtCore import QPoint, Qt
        from PySide6.QtTest import QTest

        from audapack.ui_qt.main_window import ProjectTreeView

        started = []
        orig = ProjectTreeView.startDrag
        ProjectTreeView.startDrag = lambda self_, actions: started.append(actions)
        try:
            idx = self.win.model.index_for_project_id(self.p1.id)
            rect = self.win.tree.visualRect(idx)
            center = rect.center()
            QTest.mousePress(self.win.tree.viewport(), Qt.MouseButton.LeftButton, pos=center)
            for dy in range(2, 40, 4):
                QTest.mouseMove(
                    self.win.tree.viewport(), QPoint(center.x(), center.y() - dy)
                )
            QTest.mouseRelease(
                self.win.tree.viewport(), Qt.MouseButton.LeftButton, pos=center
            )
        finally:
            ProjectTreeView.startDrag = orig
        self.assertTrue(started, "drag never started on press+move")


if __name__ == "__main__":
    unittest.main()
