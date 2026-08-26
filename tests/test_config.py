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
    migrate_legacy_data,
    redact_legacy_source_config,
    revoke_legacy_token_acceptance,
    safe_slug,
    save_config,
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
