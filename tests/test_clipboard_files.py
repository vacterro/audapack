"""Unit tests for AUDAPACK archive locator helpers and clipboard file-drop API."""

import os
import struct
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from audapack.models import Project
from audapack.packing import find_archive_for_project, find_latest_archive, pack_single


class TestFindLatestArchive(unittest.TestCase):
    def setUp(self):
        import shutil
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_returns_none_when_dir_missing(self):
        self.assertIsNone(find_latest_archive(self.tmp / "no_such_dir", "X"))

    def test_returns_none_when_no_matching_archives(self):
        self.assertIsNone(find_latest_archive(self.tmp, "NothingThere"))

    def test_returns_most_recent_matching_archive(self):
        old = self.tmp / "MyProject_01-01-2020-T00-00-00.zip"
        new = self.tmp / "MyProject_26-08-2026-T04-00-00.zip"
        other = self.tmp / "OtherProject_26-08-2026-T04-00-00.zip"
        old.write_bytes(b"a")
        new.write_bytes(b"b")
        other.write_bytes(b"c")

        # Force mtimes so ordering is deterministic.
        os.utime(old, (1577836800, 1577836800))
        os.utime(new, (1795660800, 1795660800))
        os.utime(other, (1795660800, 1795660800))

        latest = find_latest_archive(self.tmp, "MyProject")
        self.assertEqual(latest, new)

    def test_does_not_match_unrelated_stem(self):
        keep = self.tmp / "OtherProject_26-08-2026-T04-00-00.zip"
        keep.write_bytes(b"x")
        os.utime(keep, (1795660800, 1795660800))
        self.assertIsNone(find_latest_archive(self.tmp, "MyProject"))

    def test_sanitizes_stem_with_unsafe_chars(self):
        # Stem with characters that safe_archive_stem replaces (-> underscores).
        # The archive on disk must therefore already use the sanitized stem.
        safe_stem = "My_Project__"  # "My:Project?*" -> "My_Project__"
        good = self.tmp / f"{safe_stem}_26-08-2026-T04-00-00.zip"
        good.write_bytes(b"y")
        os.utime(good, (1795660800, 1795660800))

        latest = find_latest_archive(self.tmp, "My:Project?*")
        self.assertEqual(latest, good)


class TestFindArchiveForProject(unittest.TestCase):
    def setUp(self):
        import shutil
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_falls_back_through_archive_name_display_id(self):
        p = Project(
            id="myproj",
            display_name="MyProject",
            source_path="",
            archive_name="MyProject",
            audit_project_name="MyProject",
        )
        zip1 = self.tmp / "MyProject_26-08-2026-T04-00-00.zip"
        zip1.write_bytes(b"a")
        os.utime(zip1, (1795660800, 1795660800))

        self.assertEqual(find_archive_for_project(p, self.tmp), zip1)

    def test_archive_name_takes_priority_when_defined(self):
        p = Project(
            id="myproj",
            display_name="My Project Display",
            source_path="",
            archive_name="CustomArchive",
            audit_project_name="",
        )
        custom = self.tmp / "CustomArchive_26-08-2026-T04-00-00.zip"
        custom.write_bytes(b"a")
        os.utime(custom, (1795660800, 1795660800))

        # Should match CustomArchive first.
        self.assertEqual(find_archive_for_project(p, self.tmp), custom)

    def test_returns_none_when_no_archives_anywhere(self):
        p = Project(
            id="ghost",
            display_name="Ghost",
            source_path="",
            archive_name="Ghost",
            audit_project_name="",
        )
        self.assertIsNone(find_archive_for_project(p, self.tmp))


class TestPackSingleCreatesFindableArchive(unittest.TestCase):
    def setUp(self):
        import shutil
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.src = self.tmp / "src"
        self.src.mkdir()
        (self.src / "a.txt").write_text("hello", encoding="utf-8")
        self.out = self.tmp / "out"
        self.out.mkdir()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_pack_then_locate_round_trip(self):
        res = pack_single(
            source_path=self.src,
            output_dir=self.out,
            archive_stem="RoundTrip",
            excludes=set(),
            delete_old=True,
        )
        self.assertTrue(res.success)
        latest = find_latest_archive(self.out, "RoundTrip")
        self.assertIsNotNone(latest)
        self.assertEqual(latest.resolve(), res.output_path.resolve())


class TestClipboardFilesModule(unittest.TestCase):
    """The CF_HDROP helper is only meaningful on Windows; on other platforms
    it must degrade gracefully (return False) instead of raising."""

    def test_returns_false_on_non_windows(self):
        from audapack.ui import clipboard_files
        with patch.object(clipboard_files.sys, "platform", "linux"):
            self.assertFalse(clipboard_files.copy_file_to_clipboard("/tmp/a.zip"))

    def test_returns_false_for_empty_paths(self):
        from audapack.ui import clipboard_files
        self.assertFalse(clipboard_files.copy_file_to_clipboard(None))  # type: ignore[arg-type]
        self.assertFalse(clipboard_files.copy_files_to_clipboard([]))

    def test_build_payload_shape_contains_dropfiles_and_wide_paths(self):
        from audapack.ui.clipboard_files import _build_dropfiles_payload

        payload = _build_dropfiles_payload([Path("C:/some/archive.zip"), Path("D:/other.zip")])
        self.assertIsNotNone(payload)

        # DROPFILES on Windows is 20 bytes (DWORD + LONG + LONG + BOOL + BOOL; each 4 bytes by default).
        offset = 20
        self.assertGreaterEqual(len(payload), offset)
        # Validate header: offset==20, fWide==1 (Unicode), pt_x==0, pt_y==0, fNC==0.
        p_files, pt_x, pt_y, f_nc, f_wide = struct.unpack("<IIIII", payload[:offset])
        self.assertEqual(p_files, offset)
        self.assertEqual(pt_x, 0)
        self.assertEqual(pt_y, 0)
        self.assertEqual(f_nc, 0)
        self.assertEqual(f_wide, 1)

        # Rest is UTF-16LE path string with \0 separators and \0\0 terminator.
        body = payload[offset:]
        text = body.decode("utf-16-le")
        self.assertTrue(text.startswith("C:\\some\\archive.zip"))
        self.assertIn("\0D:\\other.zip", text)
        self.assertTrue(text.endswith("\0\0"))


class TestCopyFilesToClipboardWin32(unittest.TestCase):
    """Full Win32 round-trip mocked — exercises memory alloc, lock, write,
    clipboard open/empty/set/close dance."""

    def _make_mock_kernel_user(self):
        kernel = MagicMock()
        user = MagicMock()
        # Successful allocation + lock + write.
        kernel.GlobalAlloc.return_value = 12345
        kernel.GlobalLock.return_value = 9999
        kernel.GlobalFree.return_value = 0

        user.OpenClipboard.return_value = True
        user.EmptyClipboard.return_value = True
        user.SetClipboardData.return_value = 55555
        user.CloseClipboard.return_value = True
        return kernel, user

    def setUp(self):
        from audapack.ui import clipboard_files
        self.cf = clipboard_files

    def test_copy_files_returns_true_on_happy_path(self):
        kernel, user = self._make_mock_kernel_user()
        # ctypes.memmove with MagicMock int pointers would segfault; patch it as a no-op.
        with patch.object(self.cf.sys, "platform", "win32"), \
             patch.object(self.cf, "_Kernel32", kernel), \
             patch.object(self.cf, "_User32", user), \
             patch.object(self.cf.ctypes, "memmove", lambda *a, **kw: None), \
             patch.object(self.cf.ctypes, "sizeof", lambda _x: 20):
            ok = self.cf.copy_files_to_clipboard(["C:/a.zip"])
        self.assertTrue(ok)
        user.OpenClipboard.assert_called()
        user.EmptyClipboard.assert_called()
        user.SetClipboardData.assert_called_once()
        self.assertEqual(user.SetClipboardData.call_args.args[0], self.cf.CF_HDROP)

    def test_copy_files_returns_false_when_clipboard_refuses(self):
        kernel, user = self._make_mock_kernel_user()
        user.OpenClipboard.return_value = False
        with patch.object(self.cf.sys, "platform", "win32"), \
             patch.object(self.cf, "_Kernel32", kernel), \
             patch.object(self.cf, "_User32", user), \
             patch.object(self.cf.ctypes, "memmove", lambda *a, **kw: None), \
             patch.object(self.cf.ctypes, "sizeof", lambda _x: 20):
            ok = self.cf.copy_files_to_clipboard(["C:/a.zip"])
        self.assertFalse(ok)

    def test_copy_files_returns_false_when_set_clipboard_fails(self):
        kernel, user = self._make_mock_kernel_user()
        user.SetClipboardData.return_value = 0
        with patch.object(self.cf.sys, "platform", "win32"), \
             patch.object(self.cf, "_Kernel32", kernel), \
             patch.object(self.cf, "_User32", user), \
             patch.object(self.cf.ctypes, "memmove", lambda *a, **kw: None), \
             patch.object(self.cf.ctypes, "sizeof", lambda _x: 20):
            ok = self.cf.copy_files_to_clipboard(["C:/a.zip"])
        self.assertFalse(ok)

    def test_copy_file_to_clipboard_is_thin_wrapper(self):
        kernel, user = self._make_mock_kernel_user()
        # Patch copy_files_to_clipboard in-place so we can spy on its call args
        # without triggering real Win32 paths.
        original = self.cf.copy_files_to_clipboard
        with patch.object(self.cf.sys, "platform", "win32"), \
             patch.object(self.cf, "_Kernel32", kernel), \
             patch.object(self.cf, "_User32", user), \
             patch.object(self.cf, "copy_files_to_clipboard", wraps=original) as wrapped, \
             patch.object(self.cf.ctypes, "memmove", lambda *a, **kw: None), \
             patch.object(self.cf.ctypes, "sizeof", lambda _x: 20):
            ok = self.cf.copy_file_to_clipboard("C:/only.zip")
        self.assertTrue(ok)
        wrapped.assert_called_once()
        args = wrapped.call_args.args[0]
        self.assertEqual(len(args), 1)
        self.assertTrue(str(args[0]).endswith("only.zip"))


if __name__ == "__main__":
    unittest.main()