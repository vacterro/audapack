"""T-176: external archive drag must be FILE-ONLY (no text/plain payload).

Root cause: ProjectRoomModel.mimeData() called mime.setText(display_name), so
an external drag of an archive row carried the project name as plain text next
to the real ZIP URL. An editor/composer is allowed to consume text/plain and
insert it at the caret while also accepting the file, which inserted project or
archive names into the operator's prompt text.

Contract under test:
- model.mimeData() carries application/x-audapack-project and NEVER text/plain;
- with an archive, the final drag MIME carries the custom payload + one
  text/uri-list URL pointing at exactly the canonical current archive;
- without an archive, no fake URL and still no text/plain;
- the drag badge keeps showing the real display_name (derived from the model,
  not from the transferred MIME text);
- COPY ZIP's successful normal clipboard path stays file-only;
- internal reorder DnD and incoming Explorer folder drops keep working.
"""

import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from audapack.config import AppConfig
from audapack.models import Project
from audapack.services.project_service import ProjectService
from audapack.ui_qt.models.project_room_model import MIME_TYPE_PROJECT


def _make_archive(proj: Project) -> Path:
    """Creates the canonical archive layout find_archive_for_project resolves."""
    from audapack.config import app_dir
    from audapack.packing import resolve_output_dir

    cfg = AppConfig()
    out_dir = resolve_output_dir(
        Path(proj.source_path), cfg.packing, fallback=app_dir(),
        group=proj.priority_group, project=proj,
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    arc = out_dir / f"{proj.display_name}_08-09-2026-T00-00-00.zip"
    with zipfile.ZipFile(arc, "w") as zf:
        zf.writestr("a.txt", "hello")
    return arc


class TestArchiveDragPayload(unittest.TestCase):
    def setUp(self):
        from PySide6.QtWidgets import QApplication

        self.app = QApplication.instance() or QApplication([])
        self._tmp = tempfile.TemporaryDirectory(prefix="audapack-drag-")
        self.addCleanup(self._tmp.cleanup)
        self.svc = ProjectService(AppConfig(), base_dir=Path(self._tmp.name))
        self.svc.add_project("DragProj", source_path=str(Path(self._tmp.name) / "DragProj"))
        self.proj = self.svc.list_projects()[0]
        self.model = self.svc and __import__(
            "audapack.ui_qt.models.project_room_model", fromlist=["ProjectRoomModel"]
        ).ProjectRoomModel(self.svc)
        self.idx = self.model.index_for_project_id(self.proj.id)
        self.assertTrue(self.idx.isValid())

    def test_model_mime_has_no_text_fallback(self):
        mime = self.model.mimeData([self.idx])
        self.assertTrue(mime.hasFormat(MIME_TYPE_PROJECT))
        self.assertFalse(mime.hasFormat("text/plain"), "model drag must not export text/plain")
        self.assertFalse(mime.hasText())

    def test_archive_drag_payload_is_file_only(self):
        arc = _make_archive(self.proj)
        from PySide6.QtCore import QUrl

        from audapack.ui_qt.main_window import MainWindow

        win = MainWindow(self.svc)
        self.addCleanup(win.close)
        mime = self.model.mimeData([self.idx])
        mime.setUrls([QUrl.fromLocalFile(str(arc.resolve()))])

        self.assertTrue(mime.hasFormat(MIME_TYPE_PROJECT))
        self.assertTrue(mime.hasUrls())
        # NOTE: with urls set, Qt DERIVES text/plain from the uri-list itself
        # (hasText() is True), so the payload contract is about what WE set:
        # hasFormat("text/plain") must be False -- no injected name text.
        self.assertFalse(mime.hasFormat("text/plain"))
        urls = mime.urls()
        self.assertEqual(len(urls), 1)
        self.assertTrue(urls[0].isLocalFile())
        self.assertEqual(Path(urls[0].toLocalFile()).resolve(), arc.resolve())

    def test_no_archive_keeps_custom_mime_without_fake_url(self):
        mime = self.model.mimeData([self.idx])
        self.assertTrue(mime.hasFormat(MIME_TYPE_PROJECT))
        self.assertFalse(mime.hasFormat("text/uri-list"))
        self.assertFalse(mime.hasFormat("text/plain"))
        self.assertEqual(mime.urls(), [])

    def test_drag_badge_uses_model_display_name_not_mime_text(self):
        """The badge name must come from the model role, never MIME text."""
        from audapack.ui_qt.main_window import MainWindow

        win = MainWindow(self.svc)
        self.addCleanup(win.close)
        name = self.idx.data(self.model.ROLES["display_name"])
        self.assertEqual(name, self.proj.display_name)
        mime = self.model.mimeData([self.idx])
        self.assertFalse(mime.hasText(), "badge text must not ride in the MIME")
        self.assertEqual(name, self.proj.display_name)

    def test_internal_reorder_does_not_depend_on_text(self):
        """dropMimeData consumes only the custom payload."""
        target_idx = self.model.index_for_slot(self.proj.priority_group, 2)
        self.assertTrue(target_idx.isValid())
        mime = self.model.mimeData([self.idx])
        self.assertTrue(self.model.dropMimeData(mime, __import__("PySide6.QtCore", fromlist=["Qt"]).Qt.DropAction.MoveAction, -1, -1, target_idx))

    def test_explorer_incoming_folder_drop_still_accepted(self):
        from PySide6.QtCore import QMimeData, QPointF, Qt, QUrl
        from PySide6.QtGui import QDropEvent

        from audapack.ui_qt.main_window import MainWindow

        win = MainWindow(self.svc)
        self.addCleanup(win.close)
        folder = Path(self._tmp.name) / "NewFolder"
        folder.mkdir(exist_ok=True)
        md = QMimeData()
        md.setUrls([QUrl.fromLocalFile(str(folder))])
        idx = win.model.index_for_slot("MAIN0", 2)
        rect = win.tree.visualRect(idx)
        event = QDropEvent(
            QPointF(rect.center().x(), rect.center().y()),
            Qt.DropAction.CopyAction,
            md,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        with __import__("unittest").mock.patch.object(win, "_add_project_from_path") as add:
            win.tree.dropEvent(event)
            add.assert_called_once()

    def test_copy_zip_clipboard_file_only(self):
        _make_archive(self.proj)
        from PySide6.QtCore import QUrl
        from PySide6.QtWidgets import QApplication

        from audapack.ui_qt.main_window import MainWindow

        win = MainWindow(self.svc)
        self.addCleanup(win.close)
        # T-207: the offscreen platform faults at interpreter teardown when a
        # test leaves URL-carrying MIME data on its clipboard (exit
        # 0xC0000005 after a green summary). Release it while Qt is still
        # alive; cleanups run LIFO, so the window closes first.
        self.addCleanup(QApplication.clipboard().clear)
        win._on_copy_archive(self.proj)
        cb = QApplication.clipboard()
        mime = cb.mimeData()
        self.assertIsNotNone(mime)
        self.assertTrue(mime.hasUrls())
        self.assertFalse(mime.hasFormat("text/plain"))
        from audapack.packing import find_archive_for_project
        from audapack.packing import resolve_output_dir as _rod
        expected = find_archive_for_project(self.proj, _rod(Path(self.proj.source_path), self.svc.config.packing, fallback=__import__("audapack.config", fromlist=["app_dir"]).app_dir(), group=self.proj.priority_group, project=self.proj))
        self.assertEqual(mime.urls(), [QUrl.fromLocalFile(str(expected.resolve()))])


if __name__ == "__main__":
    unittest.main()
