"""Unit tests for Windows context menu registration."""

import unittest
from pathlib import Path

from audapack.context_menu import (
    get_launcher_command,
    install_context_menu,
    is_context_menu_installed,
    remove_context_menu,
)


class TestContextMenu(unittest.TestCase):
    def test_get_launcher_command(self):
        cmd = get_launcher_command(Path(r"C:\Custom Path With Spaces\AUDAPACK.pyw"))
        self.assertIn('--pack "%1"', cmd)
        self.assertIn("AUDAPACK.pyw", cmd)
        self.assertTrue(cmd.startswith('"'))

    def test_install_and_remove_mocked(self):
        from unittest import mock
        fake_reg = {}

        class FakeKey:
            def __init__(self, path):
                self.path = path

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

        def fake_create_key(root, subkey):
            if subkey not in fake_reg:
                fake_reg[subkey] = {}
            return FakeKey(subkey)

        def fake_open_key(root, subkey, reserved, access):
            if subkey not in fake_reg:
                raise OSError("Key not found")
            return FakeKey(subkey)

        def fake_set_value(key, name, reserved, type_, value):
            fake_reg[key.path][name] = value

        def fake_query_value(key, name):
            if name in fake_reg.get(key.path, {}):
                return fake_reg[key.path][name], 1
            raise OSError("Value not found")

        def fake_delete_key(root, subkey):
            if subkey in fake_reg:
                del fake_reg[subkey]
            else:
                raise OSError("Key not found")

        with mock.patch("winreg.CreateKey", side_effect=fake_create_key), \
             mock.patch("winreg.OpenKey", side_effect=fake_open_key), \
             mock.patch("winreg.SetValueEx", side_effect=fake_set_value), \
             mock.patch("winreg.QueryValueEx", side_effect=fake_query_value), \
             mock.patch("winreg.DeleteKey", side_effect=fake_delete_key), \
             mock.patch("os.name", "nt"):

            self.assertFalse(is_context_menu_installed())
            self.assertTrue(install_context_menu())
            self.assertTrue(is_context_menu_installed())
            self.assertTrue(remove_context_menu())
            self.assertFalse(is_context_menu_installed())


class TestAgentLaunchers(unittest.TestCase):
    def test_agent_launchers_invocation(self):
        from unittest.mock import patch

        from PySide6.QtWidgets import QApplication

        from audapack.models import Project
        from audapack.services.project_service import ProjectService
        from audapack.ui_qt.main_window import MainWindow

        QApplication.instance() or QApplication([])

        import shutil
        import tempfile

        from audapack.config import AppConfig

        tmp = tempfile.mkdtemp()
        try:
            cfg = AppConfig()
            svc = ProjectService(cfg, base_dir=Path(tmp))

            proj = Project(
                id="testproj",
                display_name="Test Project",
                source_path=str(Path("V:/code/testproj")),
                priority_group="MAIN0",
                slot=1,
            )

            win = MainWindow(svc)
            with patch("subprocess.Popen") as mock_popen:
                win._on_open_with_opencode(proj)
                self.assertTrue(mock_popen.called)

                mock_popen.reset_mock()
                win._on_open_with_cline(proj)
                self.assertTrue(mock_popen.called)

                mock_popen.reset_mock()
                win._on_open_with_freebuff(proj)
                self.assertTrue(mock_popen.called)

                mock_popen.reset_mock()
                win._on_open_with_codex(proj, "main_codex")
                self.assertTrue(mock_popen.called)

            # Test Copy Audit File Path (non-SAIPEN)
            audit_dir = Path(tmp) / "audits" / "testproj"
            audit_dir.mkdir(parents=True, exist_ok=True)
            audit_file = audit_dir / "AUDIT_CORE_testproj.md"
            audit_file.write_text("Test audit content", encoding="utf-8")
            cfg.audits.root = str(Path(tmp) / "audits")

            with patch.object(win._audit_service, "get_preferred_audit_file_path", return_value=audit_file):
                win._on_copy_audit_file_path(proj)
                self.assertEqual(QApplication.clipboard().text(), str(audit_file.resolve()))

                # Test Copy Audit File Path with SAIPEN detected
                saipen_proj_dir = Path(tmp) / "saipen_proj"
                (saipen_proj_dir / ".saipen").mkdir(parents=True, exist_ok=True)
                saipen_proj = Project(
                    id="saipenproj",
                    display_name="SAIPEN Project",
                    source_path=str(saipen_proj_dir),
                    priority_group="MAIN0",
                    slot=2,
                )
                win._on_copy_audit_file_path(saipen_proj)
                self.assertEqual(QApplication.clipboard().text(), f"/saipen gg {audit_file.resolve()}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestSeamlessProjectOperations(unittest.TestCase):
    def test_direct_add_move_and_delete_no_popups(self):
        import shutil
        import tempfile
        from unittest.mock import patch

        from PySide6.QtCore import QPoint, Qt
        from PySide6.QtGui import QMouseEvent
        from PySide6.QtWidgets import QApplication

        from audapack.config import AppConfig
        from audapack.services.project_service import ProjectService
        from audapack.ui_qt.main_window import MainWindow

        QApplication.instance() or QApplication([])

        tmp = tempfile.mkdtemp()
        try:
            cfg = AppConfig()
            svc = ProjectService(cfg, base_dir=Path(tmp))
            win = MainWindow(svc)

            # 1. Create a dummy project directory
            proj_dir = Path(tmp) / "MyNewApp"
            proj_dir.mkdir(parents=True, exist_ok=True)

            # Direct add from path (0 popups, auto slot assignment)
            p1 = win._add_project_from_path(proj_dir, default_group="MAIN0")
            self.assertIsNotNone(p1)
            self.assertEqual(p1.display_name, "MyNewApp")
            self.assertEqual(p1.priority_group, "MAIN0")
            self.assertEqual(p1.slot, 1)

            # Direct add second project
            proj_dir2 = Path(tmp) / "SecondApp"
            proj_dir2.mkdir(parents=True, exist_ok=True)
            p2 = win._add_project_from_path(proj_dir2, default_group="MAIN0")
            self.assertIsNotNone(p2)
            self.assertEqual(p2.slot, 2)

            # 2. Slot Move operations
            # Move Down p1 -> slot 2 (swaps with p2)
            win._on_move_project_step(p1, step=1)
            p1_moved = svc.get_project(p1.id)
            p2_moved = svc.get_project(p2.id)
            self.assertEqual(p1_moved.slot, 2)
            self.assertEqual(p2_moved.slot, 1)

            # Move to Group SIDE0
            win._on_move_project_to_group(p1, target_group="SIDE0")
            p1_side = svc.get_project(p1.id)
            self.assertEqual(p1_side.priority_group, "SIDE0")
            self.assertEqual(p1_side.slot, 1)

            # 3. Change project folder
            proj_dir3 = Path(tmp) / "RenamedApp"
            proj_dir3.mkdir(parents=True, exist_ok=True)
            with patch("PySide6.QtWidgets.QFileDialog.getExistingDirectory", return_value=str(proj_dir3)):
                win._on_change_project_folder(p1)
                p1_renamed = svc.get_project(p1.id)
                self.assertEqual(p1_renamed.display_name, "RenamedApp")
                self.assertEqual(Path(p1_renamed.source_path).resolve(), proj_dir3.resolve())

            # 4. Direct delete (0 modal confirmation popups)
            win._on_delete_project(p1)
            self.assertIsNone(svc.get_project(p1.id))
            self.assertIsNotNone(svc.get_project(p2.id))

            # 5. Test Ctrl+C shortcut and [GG] row button click
            audit_dir = Path(tmp) / "audits" / "SecondApp"
            audit_dir.mkdir(parents=True, exist_ok=True)
            audit_file = audit_dir / "AUDIT_SUPER_ALL.md"
            audit_file.write_text("# SecondApp Audit", encoding="utf-8")
            (proj_dir2 / ".saipen").mkdir(parents=True, exist_ok=True)

            with (
                patch.object(win._audit_service, "get_preferred_audit_file_path", return_value=audit_file),
                patch.object(QApplication.clipboard(), "setText") as mock_set_text,
            ):
                # Trigger via shortcut
                win.copy_shortcut.activated.emit()
                mock_set_text.assert_called_with(f"/saipen gg {audit_file.resolve()}")

                # Trigger via simulated mouse click on [GG] button
                idx = win.model.index_for_project_id(p2.id)
                rect = win.tree.visualRect(idx)
                # Position inside the [GG] button: (rect.right() - 20, rect.top() + 10)
                from PySide6.QtCore import QPoint
                from PySide6.QtGui import QMouseEvent
                click_pos = QPoint(rect.right() - 20, rect.top() + 10)
                event = QMouseEvent(
                    QMouseEvent.Type.MouseButtonPress,
                    click_pos,
                    Qt.MouseButton.LeftButton,
                    Qt.MouseButton.LeftButton,
                    Qt.KeyboardModifier.NoModifier,
                )
                with patch.object(win, "_on_copy_audit_file_path") as mock_copy_gg:
                    win.tree.mousePressEvent(event)
                    mock_copy_gg.assert_called_once()

            # 6. Test Drag & Drop: moving p2 from slot 1 to slot 4
            from PySide6.QtCore import QByteArray, QMimeData, QPointF, QUrl
            from PySide6.QtGui import QDropEvent
            from audapack.ui_qt.models.project_room_model import MIME_TYPE_PROJECT

            s1_idx = win.model.index_for_project_id(p2.id)
            mime = win.model.mimeData([s1_idx])

            # Drop onto Slot 4 of MAIN0
            s4_idx = win.model.index_for_slot("MAIN0", 4)
            s4_rect = win.tree.visualRect(s4_idx)
            drop_point = QPointF(s4_rect.center().x(), s4_rect.center().y())

            drop_event = QDropEvent(
                drop_point,
                Qt.DropAction.MoveAction,
                mime,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            )
            with patch.object(win, "_on_project_dropped") as mock_dropped:
                win.tree.dropEvent(drop_event)
                mock_dropped.assert_called_with(p2.id, "MAIN0", 4, "MAIN0", 1)

            # Test Explorer folder drop (text/uri-list)
            proj_dir4 = Path(tmp) / "DroppedApp"
            proj_dir4.mkdir(parents=True, exist_ok=True)
            uri_mime = QMimeData()
            uri_mime.setUrls([QUrl.fromLocalFile(str(proj_dir4))])

            # 7. Test Terminal launch & KeyPress navigation
            with patch("subprocess.Popen") as mock_popen:
                win._on_open_terminal(p2)
                self.assertTrue(mock_popen.called)

            from PySide6.QtGui import QKeyEvent
            # Space key -> toggle enabled state
            space_ev = QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Space, Qt.KeyboardModifier.NoModifier)
            win.tree.setCurrentIndex(win.model.index_for_project_id(p2.id))
            win.tree.keyPressEvent(space_ev)
            p2_after = svc.get_project(p2.id)
            self.assertFalse(p2_after.enabled)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()




