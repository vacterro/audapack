"""Tests for the archive output layout switch (T-26 / CORE-009).

Verifies the resolver picks the right directory for both layouts, that
PackingService.pack_project honours the chosen layout per-project, and that
the Settings dialog persists the layout to the on-disk config.
"""

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

# Make the project importable when running this file directly.
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

from PySide6.QtWidgets import QApplication  # noqa: E402

import audapack.ui_qt.dialogs.settings_dialog as dlg_mod  # noqa: E402
from audapack.config import (  # noqa: E402
    DEFAULT_OUTPUT_LAYOUT,
    OUTPUT_LAYOUT_ALONGSIDE_PROJECTS,
    OUTPUT_LAYOUT_SINGLE_FOLDER,
    AppConfig,
    PackingConfig,
    config_path,
    load_config,
    normalize_output_layout,
    save_config,
)
from audapack.models import Project  # noqa: E402
from audapack.packing import resolve_output_dir  # noqa: E402
from audapack.services.packing_service import PackingService  # noqa: E402
from audapack.ui_qt.dialogs.settings_dialog import SettingsDialog  # noqa: E402


class TestNormalizeOutputLayout(unittest.TestCase):
    def test_unknown_falls_back_to_default(self):
        self.assertEqual(normalize_output_layout("garbage"), DEFAULT_OUTPUT_LAYOUT)
        self.assertEqual(normalize_output_layout(None), DEFAULT_OUTPUT_LAYOUT)
        self.assertEqual(normalize_output_layout(""), DEFAULT_OUTPUT_LAYOUT)
        self.assertEqual(normalize_output_layout(123), DEFAULT_OUTPUT_LAYOUT)

    def test_known_values_round_trip(self):
        for v in (OUTPUT_LAYOUT_SINGLE_FOLDER, OUTPUT_LAYOUT_ALONGSIDE_PROJECTS):
            self.assertEqual(normalize_output_layout(v), v)
            self.assertEqual(normalize_output_layout(v.upper()), v)
            self.assertEqual(normalize_output_layout(f"  {v}  "), v)


class TestResolveOutputDir(unittest.TestCase):
    def setUp(self):
        self.fallback = Path("/tmp/audapack_fallback")

    def test_single_folder_with_explicit_output_dir(self):
        cfg = PackingConfig(output_dir="/data/archives", output_layout=OUTPUT_LAYOUT_SINGLE_FOLDER)
        out = resolve_output_dir("/anywhere/project", cfg, fallback=self.fallback)
        self.assertEqual(out, Path("/data/archives"))

    def test_single_folder_with_empty_output_dir_uses_fallback(self):
        cfg = PackingConfig(output_dir="", output_layout=OUTPUT_LAYOUT_SINGLE_FOLDER)
        out = resolve_output_dir("/anywhere/project", cfg, fallback=self.fallback)
        self.assertEqual(out, self.fallback)

    def test_alongside_projects_uses_source_parent(self):
        cfg = PackingConfig(output_dir="/ignored", output_layout=OUTPUT_LAYOUT_ALONGSIDE_PROJECTS)
        out = resolve_output_dir("V:/code/_PY/_FastPrompter", cfg, fallback=self.fallback)
        self.assertEqual(out, Path("V:/code/_PY"))

    def test_alongside_projects_ignores_legacy_output_dir(self):
        # alongside mode is a sibling layout: the legacy output_dir MUST NOT
        # take precedence, otherwise a forgotten single-folder setting would
        # silently override the user's explicit "alongside" choice.
        cfg = PackingConfig(output_dir="/legacy/output", output_layout=OUTPUT_LAYOUT_ALONGSIDE_PROJECTS)
        out = resolve_output_dir("C:/code/_PY/_SAIPEN", cfg, fallback=self.fallback)
        self.assertEqual(out, Path("C:/code/_PY"))

    def test_alongside_projects_falls_back_when_parent_is_root(self):
        # A path at a drive root has no usable sibling directory. The resolver
        # must NOT raise and must NOT return the source itself (W2-003): it
        # falls back to the single-folder behaviour so the pack still succeeds.
        cfg = PackingConfig(output_dir="/data/archives", output_layout=OUTPUT_LAYOUT_ALONGSIDE_PROJECTS)
        out = resolve_output_dir("V:", cfg, fallback=self.fallback)
        self.assertEqual(out, Path("/data/archives"))

    def test_legacy_config_without_layout_field(self):
        # A PackingConfig built by older code lacks output_layout entirely;
        # the resolver must treat it as the legacy single-folder default
        # (the migration in audapack.config.migrate_legacy_data does the same).
        cfg = PackingConfig(output_dir="/data/archives")
        out = resolve_output_dir("/anywhere/project", cfg, fallback=self.fallback)
        self.assertEqual(out, Path("/data/archives"))


class TestPackingServiceLayout(unittest.TestCase):
    def _make_config(self, layout: str, output_dir: str = "") -> AppConfig:
        cfg = AppConfig()
        cfg.packing = PackingConfig(output_dir=output_dir, output_layout=layout)
        cfg.projects = [
            Project(
                id="fastprompter",
                display_name="FastPrompter",
                source_path=str(_HERE / "fixtures" / "_FastPrompter"),
                enabled=True,
                priority_group="MAIN0",
                slot=1,
                archive_name="FastPrompter",
                audit_project_name="FastPrompter",
            )
        ]
        return cfg

    def test_pack_project_alongside_writes_to_source_parent(self, *args, **kwargs):
        # We don't actually run the pack here (that would touch the user's
        # disk); we verify the resolver is called with the right arguments
        # by patching resolve_output_dir and asserting the call.
        from unittest.mock import patch

        cfg = self._make_config(OUTPUT_LAYOUT_ALONGSIDE_PROJECTS)
        svc = PackingService(config=cfg)
        with patch(
            "audapack.services.packing_service.resolve_output_dir", return_value=Path("/resolved/parent")
        ) as mocked:
            with patch("audapack.services.packing_service.pack_single") as pack_single_mock:
                pack_single_mock.return_value.success = True
                svc.pack_project("fastprompter")
                self.assertTrue(mocked.called)
                # Call shape: resolve_output_dir(source_path, packing, fallback=app_dir())
                args, kwargs = mocked.call_args
                self.assertEqual(args[0], str(cfg.projects[0].source_path))
                self.assertIs(args[1], cfg.packing)
                self.assertIn("fallback", kwargs)
                self.assertTrue(str(kwargs["fallback"]))

    def test_pack_project_single_folder_uses_output_dir(self, *args, **kwargs):
        from unittest.mock import patch

        cfg = self._make_config(OUTPUT_LAYOUT_SINGLE_FOLDER, output_dir="/data/archives")
        svc = PackingService(config=cfg)
        with patch(
            "audapack.services.packing_service.resolve_output_dir", return_value=Path("/data/archives")
        ) as mocked:
            with patch("audapack.services.packing_service.pack_single") as pack_single_mock:
                pack_single_mock.return_value.success = True
                svc.pack_project("fastprompter")
                self.assertTrue(mocked.called)


class TestSettingsDialogPersistsLayout(unittest.TestCase):
    """The Settings dialog must persist the chosen layout to the on-disk
    config.json. This is the user-visible proof: switch in UI -> value in
    config file -> pack uses alongside mode.
    """

    def test_settings_dialog_persists_alongside_layout(self):
        QApplication.instance() or QApplication([])

        base_dir = _HERE / "_tmp_settings_layout"
        if base_dir.exists():
            import shutil

            shutil.rmtree(base_dir)
        base_dir.mkdir(parents=True, exist_ok=True)

        cfg = load_config(base_dir)
        cfg.packing.output_layout = OUTPUT_LAYOUT_SINGLE_FOLDER
        save_config(cfg, base_dir)

        captured: dict = {}

        def _capture_save(c, base=None):
            captured["layout"] = c.packing.output_layout
            return save_config(c, base_dir)

        with patch.object(dlg_mod, "save_config", side_effect=_capture_save):
            dlg = SettingsDialog(cfg)
            # Simulate the user choosing the alongside option in the combo box.
            idx = dlg.output_layout.findData(OUTPUT_LAYOUT_ALONGSIDE_PROJECTS)
            self.assertGreaterEqual(idx, 0, "alongside option must be present in the combo")
            dlg.output_layout.setCurrentIndex(idx)
            dlg._save()
            dlg.deleteLater()

        # The dialog must have asked save_config to persist the chosen layout.
        self.assertEqual(captured.get("layout"), OUTPUT_LAYOUT_ALONGSIDE_PROJECTS)
        # And the value must actually be on disk in the test's base_dir config.
        on_disk = json.loads(config_path(base_dir).read_text(encoding="utf-8"))
        self.assertEqual(
            on_disk["packing"]["output_layout"],
            OUTPUT_LAYOUT_ALONGSIDE_PROJECTS,
        )

    def test_legacy_config_without_layout_loads_as_default(self):
        base_dir = _HERE / "_tmp_legacy_layout"
        if base_dir.exists():
            import shutil

            shutil.rmtree(base_dir)
        base_dir.mkdir(parents=True, exist_ok=True)

        # Write a config WITHOUT the output_layout field, simulating an
        # older config.json that pre-dates the layout switch.
        legacy = {"schema_version": 2, "initialized": True, "projects": [], "packing": {"output_dir": ""}}
        (base_dir / "config.json").write_text(json.dumps(legacy), encoding="utf-8")

        cfg = load_config(base_dir)
        self.assertEqual(cfg.packing.output_layout, DEFAULT_OUTPUT_LAYOUT)


if __name__ == "__main__":
    unittest.main()
