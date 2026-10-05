"""Unit tests for configuration loading, atomic save, and schema validation."""

import copy
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from audapack.config import (
    DEFAULT_EXCLUDES,
    SCHEMA_VERSION,
    AppConfig,
    _staged_for_save,
    config_path,
    create_default_projects,
    legacy_token_acceptance_revoked,
    load_config,
    merge_launcher_edits,
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
        self.assertEqual(loaded.schema_version, SCHEMA_VERSION)
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

    def test_auto_pack_defaults_are_on_and_hourly_for_legacy_configs(self):
        """T-167 (SRC-039): the feature ships enabled at one hour."""
        cfg = AppConfig()
        self.assertTrue(cfg.packing.auto_pack_all_enabled)
        self.assertEqual(cfg.packing.auto_pack_all_interval_minutes, 60)
        loaded = load_config(self.base_dir)  # no config file at all: legacy path
        self.assertTrue(loaded.packing.auto_pack_all_enabled)
        self.assertEqual(loaded.packing.auto_pack_all_interval_minutes, 60)

    def test_auto_pack_settings_roundtrip(self):
        cfg = AppConfig()
        cfg.packing.auto_pack_all_enabled = False
        cfg.packing.auto_pack_all_interval_minutes = 90
        self.assertTrue(save_config(cfg, self.base_dir))
        loaded = load_config(self.base_dir)
        self.assertFalse(loaded.packing.auto_pack_all_enabled)
        self.assertEqual(loaded.packing.auto_pack_all_interval_minutes, 90)

    def test_auto_pack_invalid_intervals_normalize_safely(self):
        """A zero/negative/garbage value must never become a busy loop."""
        from audapack.config import _normalized_auto_pack_interval

        self.assertEqual(_normalized_auto_pack_interval(0), 60)
        self.assertEqual(_normalized_auto_pack_interval(-5), 60)
        self.assertEqual(_normalized_auto_pack_interval("garbage"), 60)
        self.assertEqual(_normalized_auto_pack_interval(None), 60)
        self.assertEqual(_normalized_auto_pack_interval(3), 5, "below the 5-minute floor clamps up")
        self.assertEqual(_normalized_auto_pack_interval(9999), 1440, "beyond a day clamps down")
        self.assertEqual(_normalized_auto_pack_interval(720), 720)

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
        """W2-007 (audit/2.md): this test used to contaminate every later one.

        It patched LOCALAPPDATA, but `get_user_runtime_dir()` prefers
        AUDAPACK_RUNTIME_DIR -- which conftest sets once for the whole SESSION.
        So the revocation marker landed in the shared canonical secrets
        directory and stayed there, and
        `AudapackBridgeHandler._legacy_token_candidates()` returns an empty list
        whenever it exists. Measured: running this test before
        `test_legacy_candidates_env_based_and_revocable` made that test fail;
        reversing the order made both pass. A green result depended on
        collection order, not on behaviour. The runtime directory this test
        mutates is now its own.
        """
        fake_local = Path(self.temp_dir) / "LOCALAPPDATA"
        fake_runtime = Path(self.temp_dir) / "runtime"
        with mock.patch.dict(os.environ, {
            "LOCALAPPDATA": str(fake_local),
            "AUDAPACK_RUNTIME_DIR": str(fake_runtime),
        }):
            self.assertFalse(legacy_token_acceptance_revoked())
            self.assertTrue(revoke_legacy_token_acceptance())
            self.assertTrue(legacy_token_acceptance_revoked())
            # The marker is under THIS test's runtime, not the session's.
            self.assertTrue(any(fake_runtime.rglob("*")), fake_runtime)

        # And it left nothing behind for the next test to inherit.
        self.assertFalse(legacy_token_acceptance_revoked())


class TestSaveConfigLeavesInputUntouchedOnFailure(unittest.TestCase):
    """CORE-002 (audit/12.md): a failed save must not mutate its own input.

    `save_config` applied `normalize_paths()` and `initialized = True` to the
    caller's LIVE object before the durable replacement, so a refused or failed
    write still changed authoritative in-memory state -- the caller kept
    operating on state that never reached disk.
    """

    def setUp(self):
        self.base_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.base_dir, ignore_errors=True)

    def _cfg(self):
        cfg = AppConfig()
        cfg.projects = []
        cfg.packing.output_dir = "C:/out/dir"
        cfg.audits.root = "C:/audits"
        cfg.ui.preferred_browser = "C:/browsers/chrome.exe"
        return cfg

    def test_failed_replace_leaves_every_input_field_unchanged(self):
        cfg = self._cfg()
        before = cfg.to_dict()
        with mock.patch(
            "audapack.config._replace_config_file_with_retry",
            side_effect=OSError("disk said no"),
        ):
            self.assertFalse(save_config(cfg, self.base_dir))
        self.assertEqual(cfg.to_dict(), before)
        self.assertFalse(cfg.initialized)
        self.assertFalse((self.base_dir / "config.json").exists())

    def test_successful_save_still_normalizes_and_marks_initialized(self):
        """The control: the old in-place effects must survive on SUCCESS."""
        cfg = self._cfg()
        self.assertTrue(save_config(cfg, self.base_dir))
        self.assertTrue(cfg.initialized)
        self.assertEqual(cfg.packing.output_dir, os.path.normpath("C:/out/dir"))
        stored = json.loads((self.base_dir / "config.json").read_text(encoding="utf-8"))
        self.assertTrue(stored["initialized"])
        self.assertEqual(stored["packing"]["output_dir"], os.path.normpath("C:/out/dir"))


class TestSettingsLauncherMergeOwnership(unittest.TestCase):
    """CORE-003 (audit/12.md): an autosave must not roll back launcher state.

    `_persist_settings` reloaded the latest config, merged the owned scalars
    correctly, and then defeated that discipline with
    `latest.launchers = self._config.launchers` -- the build-time snapshot. Any
    unrelated checkbox therefore erased the stable CLI resolution the runtime
    had persisted through `scoped_config_write` after the dialog opened.

    Launcher ownership, decided once in `merge_launcher_edits`:
      operator -> order, identity, command_template, agent_type, enabled,
                  max_instances, console colours, add, remove;
      runtime  -> resolved_command / _probe / _profile / _stage / _is_cli.
    """

    def _launcher(self, lid="opencode", **kw):
        from audapack.config import LauncherConfig

        base = dict(
            id=lid,
            name=lid.title(),
            short_label=lid[:2].upper(),
            command_template="",
            agent_type="powershell",
            enabled=True,
        )
        base.update(kw)
        return LauncherConfig(**base)

    def _resolved(self, launcher, path=r"C:\Tools\live.exe"):
        launcher.resolved_command = path
        launcher.resolved_probe = "where live"
        launcher.resolved_profile = "external-discovery"
        launcher.resolved_stage = "stable"
        launcher.resolved_is_cli = True
        return launcher

    def test_untouched_tab_would_have_rolled_back_resolution(self):
        baseline = [self._launcher("opencode"), self._launcher("cline")]
        snapshot = copy.deepcopy(baseline)
        latest = self._resolved(copy.deepcopy(baseline[0]), r"C:\Tools\live.exe")
        latest_list = [latest, copy.deepcopy(baseline[1])]

        merged = merge_launcher_edits(latest_list, snapshot, baseline)

        self.assertEqual(
            merged[0].resolved_command, r"C:\Tools\live.exe", "resolution must survive"
        )
        self.assertEqual(merged[0].resolved_profile, "external-discovery")
        self.assertEqual(merged[0].resolved_stage, "stable")

    def test_operator_enabled_toggle_and_external_resolution_both_survive(self):
        baseline = [self._launcher("opencode", enabled=True)]
        snapshot = copy.deepcopy(baseline)
        snapshot[0].enabled = False
        latest_list = [self._resolved(self._launcher("opencode", enabled=True))]

        merged = merge_launcher_edits(latest_list, snapshot, baseline)

        self.assertFalse(merged[0].enabled, "operator edit must land")
        self.assertEqual(merged[0].resolved_command, r"C:\Tools\live.exe")

    def test_reorder_is_an_operator_edit_that_keeps_resolution(self):
        baseline = [self._launcher("a1"), self._launcher("b1")]
        snapshot = [copy.deepcopy(baseline[1]), copy.deepcopy(baseline[0])]
        latest_list = [
            self._resolved(self._launcher("a1")),
            self._resolved(self._launcher("b1"), r"C:\Tools\other.exe"),
        ]

        merged = merge_launcher_edits(latest_list, snapshot, baseline)

        self.assertEqual([lc.id for lc in merged], ["b1", "a1"])
        self.assertEqual(merged[0].resolved_command, r"C:\Tools\other.exe")
        self.assertEqual(merged[1].resolved_command, r"C:\Tools\live.exe")

    def test_intentional_command_change_clears_only_the_resolution(self):
        baseline = [self._launcher("opencode")]
        snapshot = copy.deepcopy(baseline)
        snapshot[0].command_template = r"C:\Other\opencode.exe {{path}}"
        latest_list = [self._resolved(self._launcher("opencode"))]

        merged = merge_launcher_edits(latest_list, snapshot, baseline)

        self.assertEqual(merged[0].command_template, r"C:\Other\opencode.exe {{path}}")
        self.assertEqual(merged[0].resolved_command, "", "stale answer must be dropped")
        self.assertEqual(merged[0].resolved_profile, "")

    def test_agent_type_change_also_invalidates_resolution(self):
        baseline = [self._launcher("opencode")]
        snapshot = copy.deepcopy(baseline)
        snapshot[0].agent_type = "cmd"
        latest_list = [self._resolved(self._launcher("opencode"))]

        merged = merge_launcher_edits(latest_list, snapshot, baseline)

        self.assertEqual(merged[0].agent_type, "cmd")
        self.assertEqual(merged[0].resolved_command, "")

    def test_remove_is_structural_but_an_external_add_is_never_deleted(self):
        baseline = [self._launcher("a1"), self._launcher("b1")]
        snapshot = [copy.deepcopy(baseline[0])]           # operator removed b1
        external = self._launcher("c1")
        latest_list = [
            copy.deepcopy(baseline[0]),
            copy.deepcopy(baseline[1]),
            external,
        ]

        merged = merge_launcher_edits(latest_list, snapshot, baseline)

        self.assertEqual([lc.id for lc in merged], ["a1", "c1"])

    def test_added_launcher_is_taken_from_the_dialog(self):
        baseline = [self._launcher("a1")]
        snapshot = [copy.deepcopy(baseline[0]), self._launcher("new", name="New")]
        latest_list = [copy.deepcopy(baseline[0])]

        merged = merge_launcher_edits(latest_list, snapshot, baseline)

        self.assertEqual([lc.id for lc in merged], ["a1", "new"])
        self.assertEqual(merged[1].name, "New")


class TestLegacyMediaExcludeUpgrade(unittest.TestCase):
    """CORE-002 (audit/6.md): the pattern layer pre-empted the fidelity layer.

    `build_fidelity_plan` matches configured excludes before `media_class_for`,
    so every `*.wav`/`*.mp4`/`*.woff` AUDAPACK had injected into `excludes`
    made FULL silently lossy while the manifest still said `full_snapshot`, and
    denied COMPACT/STANDARD/DEEP any media to sample. The patterns now belong to
    audapack.fidelity, and a config persisted below schema 3 is upgraded once.
    """

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.base_dir = Path(self.temp_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _legacy_excludes(self) -> list[str]:
        from audapack.config import LEGACY_MEDIA_DEFAULT_EXCLUDES

        return ["node_modules", "*.log", *sorted(LEGACY_MEDIA_DEFAULT_EXCLUDES)]

    def _write_config(self, packing: dict, schema_version: int = 2) -> Path:
        cfg_file = self.base_dir / "config.json"
        cfg_file.write_text(
            json.dumps({
                "schema_version": schema_version,
                "initialized": True,
                "projects": [],
                "packing": packing,
            }),
            encoding="utf-8",
        )
        return cfg_file

    def test_the_shipped_defaults_no_longer_own_media_or_fonts(self):
        from audapack.config import DEFAULT_EXCLUDES, LEGACY_MEDIA_DEFAULT_EXCLUDES

        overlap = LEGACY_MEDIA_DEFAULT_EXCLUDES & {p.lower() for p in DEFAULT_EXCLUDES}
        self.assertEqual(overlap, set(), f"fidelity-owned patterns still shipped: {sorted(overlap)}")

        # The example an operator copies must describe the same behaviour as the
        # executable default; the two disagreed for the whole of T-147.
        example = json.loads(
            (Path(__file__).resolve().parent.parent / "config.example.json").read_text(encoding="utf-8")
        )
        example_media = LEGACY_MEDIA_DEFAULT_EXCLUDES & {
            str(p).lower() for p in example.get("packing", {}).get("excludes", [])
        }
        self.assertEqual(example_media, set(), f"config.example.json still owns: {sorted(example_media)}")
        self.assertEqual(example.get("schema_version"), SCHEMA_VERSION)

    def test_fresh_defaults_do_not_exclude_generic_bin(self):
        self.assertNotIn("*.bin", DEFAULT_EXCLUDES)
        self.assertNotIn("*.bin", AppConfig().packing.excludes)

    def test_a_legacy_config_is_upgraded_once_and_user_patterns_survive(self):
        cfg_file = self._write_config({"output_dir": "", "excludes": self._legacy_excludes()})

        loaded = load_config(self.base_dir)
        self.assertNotIn("*.wav", loaded.packing.excludes)
        self.assertNotIn("*.woff2", loaded.packing.excludes)
        self.assertEqual(
            loaded.packing.excludes, ["node_modules", "*.log"],
            "only the historical media block may go, and order must not change",
        )

        # Persisting stamps the new schema, so the upgrade is not re-derived
        # from a legacy list forever.
        self.assertTrue(save_config(loaded, self.base_dir))
        on_disk = json.loads(cfg_file.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["schema_version"], SCHEMA_VERSION)
        self.assertEqual(on_disk["packing"]["excludes"], ["node_modules", "*.log"])
        self.assertEqual(load_config(self.base_dir).packing.excludes, ["node_modules", "*.log"])

    def test_a_legacy_binary_default_is_removed_only_with_exact_provenance(self):
        from audapack.config import LEGACY_BINARY_DEFAULT_EXCLUDES

        self._write_config({
            "output_dir": "",
            "excludes": sorted(LEGACY_BINARY_DEFAULT_EXCLUDES),
        }, schema_version=3)
        loaded = load_config(self.base_dir)
        self.assertNotIn("*.bin", loaded.packing.excludes)
        self.assertEqual(loaded.schema_version, SCHEMA_VERSION)

        # Saving the migrated config stamps the new schema; a second load is a
        # no-op and does not keep rewriting the exclude list.
        first_disk = json.loads((self.base_dir / "config.json").read_text(encoding="utf-8"))
        self.assertTrue(save_config(loaded, self.base_dir))
        second_disk = json.loads((self.base_dir / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(first_disk["packing"]["excludes"], second_disk["packing"]["excludes"])
        self.assertEqual(load_config(self.base_dir).packing.excludes, loaded.packing.excludes)

    def test_a_custom_binary_rule_is_preserved_when_provenance_is_ambiguous(self):
        self._write_config({
            "output_dir": "",
            "excludes": ["*.bin"],
        }, schema_version=3)
        loaded = load_config(self.base_dir)
        self.assertIn("*.bin", loaded.packing.excludes)

    def test_a_media_pattern_the_operator_typed_is_never_removed(self):
        """Provenance: without the whole historical block, the rule is theirs."""
        self._write_config({"output_dir": "", "excludes": ["node_modules", "*.wav"]})

        loaded = load_config(self.base_dir)
        self.assertEqual(loaded.packing.excludes, ["node_modules", "*.wav"])

    def test_an_upgraded_config_is_idempotent_across_two_round_trips(self):
        self._write_config({"output_dir": "", "excludes": self._legacy_excludes()})

        first = load_config(self.base_dir).packing.excludes
        self.assertTrue(save_config(load_config(self.base_dir), self.base_dir))
        second = load_config(self.base_dir).packing.excludes
        self.assertEqual(first, second)

    def test_full_preserves_media_a_legacy_config_used_to_drop(self):
        from audapack.fidelity import PROFILE_FULL, build_plan_from_config

        source = self.base_dir / "proj"
        source.mkdir(parents=True)
        (source / "main.py").write_text("print('x')", encoding="utf-8")
        (source / "sound.wav").write_bytes(b"\x00" * 1024)
        (source / "clip.mp4").write_bytes(b"\x00" * 1024)
        (source / "inter.woff2").write_bytes(b"\x00" * 1024)

        self._write_config({
            "output_dir": "",
            "excludes": self._legacy_excludes(),
            "fidelity_profile": PROFILE_FULL,
        })
        packing = load_config(self.base_dir).packing
        plan = build_plan_from_config(source, packing, set(packing.excludes))

        self.assertEqual(plan.archive_semantics, "full_snapshot")
        self.assertEqual(plan.excluded, 0, "FULL declaring a full snapshot may omit nothing")
        for rel in ("sound.wav", "clip.mp4", "inter.woff2"):
            self.assertTrue(plan.decisions[rel].include, rel)

    def test_always_exclude_still_wins_over_the_profile(self):
        from audapack.fidelity import (
            PROFILE_FULL,
            REASON_CONFIGURED_IGNORE,
            build_plan_from_config,
        )

        source = self.base_dir / "proj"
        source.mkdir(parents=True)
        (source / "main.py").write_text("print('x')", encoding="utf-8")
        (source / "sound.wav").write_bytes(b"\x00" * 1024)

        self._write_config({
            "output_dir": "",
            "excludes": self._legacy_excludes(),
            "fidelity_profile": PROFILE_FULL,
            "always_exclude": ["*.wav"],
        })
        packing = load_config(self.base_dir).packing
        plan = build_plan_from_config(source, packing, set(packing.excludes))

        decision = plan.decisions["sound.wav"]
        self.assertFalse(decision.include, "an explicit always_exclude must still win")
        self.assertEqual(decision.reason, REASON_CONFIGURED_IGNORE)


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


class TestFreeBuffMultiInstanceMigration(unittest.TestCase):
    """T-230: retire the historical product-owned FreeBuff single-instance cap.

    schema < 5 plus ``freebuff.max_instances == 1`` means AUDAPACK's old forced
    default; it is rewritten to 0 exactly once. Every other value, every other
    launcher id and every schema-5 explicit value is preserved.
    """

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.base_dir = Path(self.temp_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _write_config(self, schema_version, max_instances, launcher_id="freebuff"):
        launcher = {
            "id": launcher_id,
            "name": launcher_id.title(),
            "short_label": "XX",
            "command_template": "",
            "agent_type": "powershell",
            "enabled": True,
        }
        if max_instances is not None:
            launcher["max_instances"] = max_instances
        payload = {
            "schema_version": schema_version,
            "initialized": True,
            "projects": [],
            "launchers": [launcher],
        }
        if max_instances is None:
            launcher.pop("max_instances", None)
        (self.base_dir / "config.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )

    def _loaded_freebuff(self):
        cfg = load_config(self.base_dir)
        return next(lc for lc in cfg.launchers if lc.id == "freebuff")

    def test_schema4_freebuff_one_migrates_to_unlimited(self):
        self._write_config(4, 1)
        cfg = load_config(self.base_dir)
        self.assertEqual(cfg.schema_version, SCHEMA_VERSION)
        self.assertEqual(next(lc for lc in cfg.launchers if lc.id == "freebuff").max_instances, 0)
        # Persisted once: a reload sees schema 5 and the value stays 0.
        self.assertEqual(self._loaded_freebuff().max_instances, 0)

    def test_schema4_freebuff_zero_stays_zero(self):
        self._write_config(4, 0)
        self.assertEqual(self._loaded_freebuff().max_instances, 0)

    def test_schema4_freebuff_three_stays_three(self):
        self._write_config(4, 3)
        self.assertEqual(self._loaded_freebuff().max_instances, 3)

    def test_schema5_explicit_one_is_preserved(self):
        """An operator may deliberately restore the cap after migration."""
        self._write_config(5, 1)
        self.assertEqual(self._loaded_freebuff().max_instances, 1)

    def test_legacy_freebuff_without_capacity_defaults_to_zero(self):
        self._write_config(4, None)
        self.assertEqual(self._loaded_freebuff().max_instances, 0)

    def test_invalid_capacity_falls_back_to_zero(self):
        self._write_config(4, "garbage")
        self.assertEqual(self._loaded_freebuff().max_instances, 0)

    def test_migration_never_touches_another_launcher(self):
        self._write_config(4, 1, launcher_id="opencode")
        cfg = load_config(self.base_dir)
        self.assertEqual(next(lc for lc in cfg.launchers if lc.id == "opencode").max_instances, 1)



class TestStagedSaveSurvivesAForwardingMember:
    """CORE-002 regression: the staged copy must not go through the pickle
    protocol. A member that forwards unknown attributes to a private field makes
    `copy.deepcopy` recurse forever, and `save_config` swallowed the
    RecursionError into a plain `False` -- a silent no-op save.
    """

    class _Forwarding:
        def __init__(self, inner):
            self._inner = inner
            for name, value in vars(inner).items():
                setattr(self, name, value)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    def test_save_config_still_writes_with_a_forwarding_project(self, tmp_path):
        from audapack.models import Project

        cfg = AppConfig(
            projects=[self._Forwarding(Project(id="a", display_name="A",
                                               source_path=str(tmp_path / "a")))],
        )
        assert save_config(cfg, tmp_path) is True
        assert config_path(tmp_path).exists()

    def test_the_staged_copy_is_independent_of_the_caller(self, tmp_path):
        from audapack.models import Project

        staged = _staged_for_save(AppConfig())
        staged.projects.append(
            Project(id="x", display_name="X", source_path="C:/x"))
        staged.packing.output_dir = "C:/elsewhere"
        assert staged.projects[0].source_path == "C:/x"
        assert not hasattr(staged.packing, "_never_set")
