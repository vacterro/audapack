"""Unit tests for AUDAPACK UI i18n module."""

import unittest

from audapack.ui import i18n


class TestI18nCore(unittest.TestCase):
    def setUp(self):
        # Reset to known default state for deterministic assertions.
        i18n.set_language("ru")

    def tearDown(self):
        i18n.set_language("ru")

    def test_default_language_is_ru(self):
        self.assertEqual(i18n.get_language(), "ru")

    def test_t_returns_translated_string(self):
        i18n.set_language("ru")
        self.assertEqual(i18n.t("btn.pack"), "ПАК")

    def test_t_falls_back_to_english_when_missing_in_active(self):
        # Force a key that exists only in English to be missing in RU.
        i18n.TRANSLATIONS_RU["_missing_only_in_en"] = "REMOVED"  # ensure present then remove
        del i18n.TRANSLATIONS_RU["_missing_only_in_en"]
        # No translation registered anywhere.
        self.assertEqual(i18n.t("__definitely_missing_key__"), "__definitely_missing_key__")

    def test_t_uses_english_fallback_for_unknown_locale_key(self):
        # Simulate by removing a key from RU temporarily.
        saved = i18n.TRANSLATIONS_RU.pop("btn.pack", None)
        try:
            i18n.set_language("ru")
            self.assertEqual(i18n.t("btn.pack"), "PACK")  # falls back to en
        finally:
            if saved is not None:
                i18n.TRANSLATIONS_RU["btn.pack"] = saved

    def test_t_format_kwargs(self):
        self.assertEqual(i18n.t("status.pack_ok_fmt", name="X", files=3, size="1.0 MB"), "OK: X -> 3 файлов (1.0 MB)")
        i18n.set_language("en")
        self.assertEqual(i18n.t("status.pack_ok_fmt", name="X", files=3, size="1.0 MB"), "OK: X -> 3 files (1.0 MB)")

    def test_set_language_normalizes_invalid_input(self):
        applied = i18n.set_language("JA-JP")
        self.assertEqual(applied, "ru")  # unknown -> default

    def test_set_language_accepts_full_and_short_codes(self):
        i18n.set_language("EN")
        self.assertEqual(i18n.get_language(), "en")
        i18n.set_language("ru_RU")
        self.assertEqual(i18n.get_language(), "ru")

    def test_set_language_invokes_reload_callbacks(self):
        seen: list[str] = []
        def cb(lang: str) -> None:
            seen.append(lang)
        i18n.register_reload_callback(cb)
        try:
            i18n.set_language("en")
            self.assertIn("en", seen)
            # Same language should NOT fire callback.
            seen.clear()
            i18n.set_language("en")
            self.assertEqual(seen, [])
        finally:
            i18n.unregister_reload_callback(cb)

    def test_language_display_name(self):
        self.assertEqual(i18n.language_display_name("ru"), "RU")
        self.assertEqual(i18n.language_display_name("en"), "EN")
        self.assertEqual(i18n.language_display_name("zz"), "ZZ")

    def test_available_languages_lists_ru_and_en(self):
        langs = set(i18n.available_languages())
        self.assertIn("ru", langs)
        self.assertIn("en", langs)


class TestI18nParity(unittest.TestCase):
    """Every key defined in Russian MUST also exist in English (so we never
    have a fully untranslated label). This is a sanity fence against
    merge/typo regressions."""

    def test_translation_parity(self):
        ru = set(i18n.TRANSLATIONS_RU.keys())
        en = set(i18n.TRANSLATIONS_EN.keys())
        missing_in_en = ru - en
        missing_in_ru = en - ru
        self.assertEqual(missing_in_en, set(), f"Keys only in RU: {sorted(missing_in_en)}")
        self.assertEqual(missing_in_ru, set(), f"Keys only in EN: {sorted(missing_in_ru)}")


if __name__ == "__main__":
    unittest.main()
