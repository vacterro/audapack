"""Unit tests for Windows context menu registration."""

import os
import sys
import unittest
from pathlib import Path

from audapack.context_menu import (
    CONTEXT_MENU_TITLE,
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


if __name__ == "__main__":
    unittest.main()
