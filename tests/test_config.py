"""Unit tests for configuration loading, atomic save, and schema validation."""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from audapack.config import (
    AppConfig,
    create_default_projects,
    legacy_token_acceptance_revoked,
    load_config,
    redact_legacy_source_config,
    revoke_legacy_token_acceptance,
    safe_slug,
    save_config,
    scoped_config_write,
)


class TestConfig(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.base_dir = Path(self.temp_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_safe_slug(self):
        self.assertEqual(safe_slug("Smart VAC Cleaner"), "smart_vac_cleaner")
        self.assertEqual(safe_slug("  AI ChatButtons!  "), "ai_chatbuttons")
        self.assertEqual(safe_slug(""), "project")

    def test_save_and_load_config_roundtrip(self):
        cfg = AppConfig()
        cfg.projects = create_default_projects()
        cfg.packing.output_dir = str(self.base_dir / "out")
        cfg.audits.root = str(self.base_dir / "audits")

        ok = save_config(cfg, self.base_dir)
        self.assertTrue(ok)

        loaded = load_config(self.base_dir)
        self.assertEqual(loaded.schema_version, 2)
        self.assertEqual(len(loaded.projects), len(cfg.projects))
        self.assertEqual(loaded.packing.output_dir, str(self.base_dir / "out"))
        self.assertEqual(loaded.audits.root, str(self.base_dir / "audits"))

    def test_compact_rows_setting_roundtrip(self):
        cfg = AppConfig()
        cfg.ui.compact_rows = True
        self.assertTrue(save_config(cfg, self.base_dir))
        self.assertTrue(load_config(self.base_dir).ui.compact_rows)

    def test_include_timestamp_false_survives_save_load_roundtrip(self):
        cfg = AppConfig()
        cfg.packing.include_timestamp = False
        self.assertTrue(save_config(cfg, self.base_dir))
        loaded = load_config(self.base_dir)
        self.assertFalse(loaded.packing.include_timestamp, "user unchecked include_timestamp; restart must not revert it")

    def test_tooltip_duration_default_is_single_consistent_value(self):
        cfg = AppConfig()
        self.assertEqual(cfg.ui.tooltip_duration_ms, 10000)
        loaded = load_config(self.base_dir)
        self.assertEqual(loaded.ui.tooltip_duration_ms, 10000)

    def test_removed_dead_tooltip_settings_are_not_persisted(self):
        cfg = AppConfig()
        self.assertTrue(save_config(cfg, self.base_dir))
        raw = (self.base_dir / "config.json").read_text(encoding="utf-8")
        for dead in ("tooltip_style", "tooltip_delay_ms", "compact_tooltips"):
            self.assertNotIn(dead, raw, f"dead setting {dead} must not persist")

    def test_show_tooltips_setting_roundtrip(self):
        cfg = AppConfig()
        cfg.ui.show_tooltips = False
        self.assertTrue(save_config(cfg, self.base_dir))
        self.assertFalse(load_config(self.base_dir).ui.show_tooltips)

    def test_scoped_config_write_preserves_concurrent_external_mutation(self):
        """W2-007: a stale UI snapshot must never overwrite newer concurrent
        project-registry mutations; scoped write reloads the latest under the
        lock and applies only the owned fields."""
        cfg = AppConfig()
        cfg.projects = create_default_projects()
        cfg.ui.ui_language = "en"
        cfg.packing.output_dir = str(self.base_dir / "out")
        self.assertTrue(save_config(cfg, self.base_dir))

        # External writer transactionally adds a project (simulating Bridge/CLI).
        from audapack.projects import ProjectRegistry
        ext_cfg = load_config(self.base_dir)
        reg = ProjectRegistry(ext_cfg, base_dir=self.base_dir, transactional=True)
        reg.add_project("ExtProj", r"C:\ExtProj", priority_group="SIDE1")
        on_disk = load_config(self.base_dir)
        self.assertGreater(len(on_disk.projects), len(cfg.projects))

        # A stale UI snapshot tries to persist ONLY its owned fields.
        ok = scoped_config_write(
            lambda latest: setattr(latest.ui, "ui_language", "ru"),
            base_dir=self.base_dir,
        )
        self.assertTrue(ok)
        merged = load_config(self.base_dir)
        self.assertEqual(merged.ui.ui_language, "ru", "owned field must be applied")
        self.assertEqual(len(merged.projects), len(on_disk.projects), "external project mutation must survive")

    def test_corrupted_config_fails_closed(self):
        c_file = self.base_dir / "audapack.json"
        c_file.write_text("{ broken json", encoding="utf-8")

        with self.assertRaises(ValueError):
            load_config(self.base_dir)

    def test_serialized_portable_config_is_secret_free(self):
        cfg = AppConfig()
        cfg.bridge.token = "supersecret_production_token_value"

        data = cfg.to_dict()
        self.assertNotIn("token", data["bridge"])

        ok = save_config(cfg, self.base_dir)
        self.assertTrue(ok)
        raw = (self.base_dir / "config.json").read_text(encoding="utf-8")
        self.assertNotIn("supersecret_production_token_value", raw)

    def test_legacy_token_in_loaded_config_is_scrubbed_once(self):
        secret = "legacy_migrated_token_value_123456"
        cfg_file = self.base_dir / "config.json"
        cfg_file.write_text(
            json.dumps({"bridge": {"host": "127.0.0.1", "port": 19999, "token": secret}}),
            encoding="utf-8",
        )

        loaded = load_config(self.base_dir)
        self.assertEqual(loaded.bridge.token, secret)  # runtime connectivity preserved

        raw = cfg_file.read_text(encoding="utf-8")
        self.assertNotIn(secret, raw)  # portable config scrubbed

        again = load_config(self.base_dir)
        self.assertEqual(again.bridge.token, secret)  # served from canonical secret file

    def test_redact_legacy_source_config(self):
        src = self.base_dir / "audapack.json"
        src.write_text(
            json.dumps({"bridge": {"token": "live_secret_123456"}, "bridge_token": "alt_secret_123456"}),
            encoding="utf-8",
        )
        self.assertTrue(redact_legacy_source_config(src))
        data = json.loads(src.read_text(encoding="utf-8"))
        self.assertEqual(data["bridge"]["token"], "")
        self.assertEqual(data["bridge_token"], "")

        already_clean = self.base_dir / "clean.json"
        already_clean.write_text(json.dumps({"bridge": {"port": 1}}), encoding="utf-8")
        self.assertTrue(redact_legacy_source_config(already_clean))

    def test_legacy_token_acceptance_marker_roundtrip(self):
        fake_local = Path(self.temp_dir) / "LOCALAPPDATA"
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(fake_local)}):
            self.assertFalse(legacy_token_acceptance_revoked())
            self.assertTrue(revoke_legacy_token_acceptance())
            self.assertTrue(legacy_token_acceptance_revoked())


if __name__ == "__main__":
    unittest.main()


class TestToolbarButtonVisibility(unittest.TestCase):
    """The 640px row is a budget, and the operator decides how to spend it."""

    def test_the_default_hides_the_buttons_that_have_another_route(self):
        from audapack.config import DEFAULT_HIDDEN_TOOLBAR_BUTTONS, UIConfig

        self.assertEqual(tuple(DEFAULT_HIDDEN_TOOLBAR_BUTTONS), ("GG", "IA", "IA+"))
        self.assertEqual(UIConfig().hidden_toolbar_buttons, ["GG", "IA", "IA+"])

    def test_every_default_hidden_button_exists(self):
        from audapack.config import DEFAULT_HIDDEN_TOOLBAR_BUTTONS, TOOLBAR_BUTTON_KEYS

        for key in DEFAULT_HIDDEN_TOOLBAR_BUTTONS:
            self.assertIn(key, TOOLBAR_BUTTON_KEYS)

    def test_an_unknown_key_is_dropped_rather_than_carried_forever(self):
        from audapack.config import _normalized_hidden_toolbar_buttons

        self.assertEqual(_normalized_hidden_toolbar_buttons(["gg", "retired", "IA"]), ["GG", "IA"])

    def test_an_unreadable_value_falls_back_to_the_default(self):
        from audapack.config import _normalized_hidden_toolbar_buttons

        self.assertEqual(_normalized_hidden_toolbar_buttons("nonsense"), ["GG", "IA", "IA+"])

    def test_hiding_nothing_is_a_real_choice(self):
        """An empty list means "show everything" and must survive a round trip."""
        from audapack.config import _normalized_hidden_toolbar_buttons

        self.assertEqual(_normalized_hidden_toolbar_buttons([]), [])

    def test_the_choice_survives_a_config_round_trip(self):
        import tempfile
        from pathlib import Path

        from audapack.config import load_config, save_config

        with tempfile.TemporaryDirectory() as tmp:
            old = os.environ.get("AUDAPACK_RUNTIME_DIR")
            os.environ["AUDAPACK_RUNTIME_DIR"] = str(Path(tmp) / "runtime")
            try:
                config = load_config()
                config.ui.hidden_toolbar_buttons = ["ZIP"]
                save_config(config)
                self.assertEqual(load_config().ui.hidden_toolbar_buttons, ["ZIP"])
            finally:
                if old is None:
                    os.environ.pop("AUDAPACK_RUNTIME_DIR", None)
                else:
                    os.environ["AUDAPACK_RUNTIME_DIR"] = old
