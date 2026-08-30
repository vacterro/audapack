"""Lightweight i18n module for AUDAPACK UI.

Translations cover all user-visible labels (buttons, dialogs, statuses, menus).
Two locales are bundled:
  * ``ru`` — Russian (default).
  * ``en`` — English.

Fallback chain for any key: requested locale -> English -> key itself.
This guarantees a usable string is always returned even if a translation is
incomplete.

Selection persists through ``AppConfig.ui.ui_language``. ``set_language`` updates
the module-level current locale and triggers a registered reload callback so
the UI can re-translate every visible widget without restart.
"""

from __future__ import annotations

from typing import Callable, Optional

SUPPORTED_LANGUAGES = ("ru", "en")

DEFAULT_LANGUAGE = "ru"

# Russian (default) translations. Keys are dot-namespaced, e.g. "btn.pack_all".
TRANSLATIONS_RU: dict[str, str] = {
    # Top toolbar
    "toolbar.pack_all": "УПАКОВАТЬ ВСЁ",
    "toolbar.refresh": "ОБНОВИТЬ АУДИТЫ",
    "toolbar.paste_audit": "ВСТАВИТЬ АУДИТ (Ctrl+V)",
    "toolbar.open_output": "ОТКРЫТЬ ПАПКУ",
    "toolbar.settings": "НАСТРОЙКИ",
    "toolbar.lang": "ЯЗЫК",

    # Header row
    "header.slot": "СЛОТ",
    "header.project": "Проект и путь",
    "header.wave": "ВОЛНА",
    "header.temperature": "СВЕЖЕСТЬ",
    "header.handoff": "АУДИТ",
    "header.pack": "СБОРКА",
    "header.archive": "АРХИВ",
    "header.menu": "···",

    # Archive freshness / pack progress (Qt delegate)
    "archive.fresh.tag": "  [✓]",
    "archive.stale.tag": "  [·]",
    "archive.old.tag": "  [!]",
    "archive.source_newer.tag": "  [SRC▲]",
    "pack.progress_fmt": "[УПАК {pct}% {files}ф {size}]",

    # Project row - checkbox / slot
    "row.missing_path": "[НЕТ ПУТИ]",
    "row.dirty_fmt": "[ГРЯЗНО {n}]",
    "row.clean": "[ЧИСТО]",
    "row.saipen": "[SAIPEN]",
    "row.empty_slot_fmt": "[ ПУСТОЙ СЛОТ {group} #{slot} ]",
    "row.add_project": "+ ДОБАВИТЬ ПРОЕКТ",
    "row.muted_badge": "[ПРИГЛУШЁН]",
    "row.ignore_tooltip": "Приглушить визуально (не отменяет упаковку)",

    # Audit column
    "audit.ready_all": "3/3",
    "audit.ready_partial_fmt": "{n}/3",
    "audit.ready_none": "0/3",

    # Copy audit button
    "btn.copy_audit": "АУДИТ",
    "btn.copy_audit_done": "✓ АУДИТ",
    "btn.copy_audit_disabled_hint": "АУДИТ НЕ ГОТОВ",

    # Pack button
    "btn.pack": "ПАК",
    "btn.pack_disabled_hint": "ПУТЬ ОТСУТСТВУЕТ",

    # Copy archive button (new)
    "btn.copy_archive": "АРХИВ",
    "btn.copy_archive_done": "✓ АРХИВ",
    "btn.copy_archive_no_archive": "НЕТ",
    "btn.copy_archive_disabled_hint": "АРХИВ ОТСУТСТВУЕТ",

    # Menu
    "menu.move_up": "▲ Вверх",
    "menu.move_down": "▼ Вниз",
    "menu.move_to_group": "В группу...",
    "menu.move_to_group_fmt": "→ {group}",
    "menu.move_dialog": "Слот / группа (диалог)...",
    "menu.edit": "Изменить проект...",
    "menu.delete": "Удалить проект",
    "menu.paste_audit": "Вставить аудит из буфера",
    "menu.mute_project": "Приглушить (визуально)",
    "menu.unmute_project": "Снять приглушение",
    "menu.copy_archive": "Скопировать архив",
    "menu.open_audit_dir": "Открыть папку аудита",

    # Bottom status
    "status.ready": "Готово",
    "status.cancelling": "Отмена...",
    "status.paste_audit_ok_fmt": "✓ Аудит сохранён: {msg}",
    "status.paste_audit_fail_fmt": "Ошибка вставки аудита: {err}",
    "status.packing_fmt": "[{i}/{n}] Упаковка {name}...",
    "status.pack_progress_fmt": "Упаковка: {files} файлов ({size}) | {file}",
    "status.pack_ok_fmt": "OK: {name} -> {files} файлов ({size})",
    "status.pack_fail_fmt": "ОШИБКА: {name}: {err}",
    "status.pack_finished": "Упаковка завершена.",
    "status.pack_cancelled": "Упаковка отменена.",
    "status.pack_starting": "Запуск упаковки...",
    "status.copied_audit_fmt": "Скопирован ALL_3 для {name} ({n} символов)",
    "status.copied_archive_fmt": "Архив скопирован в буфер: {name}",
    "status.copy_archive_no_archive_fmt": "Архив не найден для {name}",
    "status.no_enabled_projects": "Нет активных проектов для упаковки.",
    "status.language_switched_fmt": "Язык переключён на {lang}",
    "status.group_projects_count_fmt": "({n} / {total} проектов)",

    # Dialogs
    "dialog.add_title": "Добавить проект",
    "dialog.edit_title": "Изменить проект",
    "dialog.field.display_name": "Название:",
    "dialog.field.source_path": "Путь к исходникам:",
    "dialog.field.priority_group": "Группа приоритета:",
    "dialog.field.audit_override": "Имя аудита:",
    "dialog.browse": "Обзор...",
    "dialog.browse_dir_title": "Выберите папку проекта",
    "dialog.save_add": "Добавить",
    "dialog.save_edit": "Сохранить",
    "dialog.cancel": "Отмена",
    "dialog.confirm_remove_fmt": "Удалить проект '{name}' из слота {group} #{slot}?",

    # Settings dialog
    "settings.title": "AUDAPACK — Настройки и компоненты",
    "settings.tab.packing": "Упаковка и аудит",
    "settings.tab.bridge": "Мост и автозапуск",
    "settings.output_dir": "Папка для архивов (по умолчанию Desktop/PACK):",
    "settings.audit_root": "Корень аудитов (AUDITING_IMPLEMENTATION):",
    "settings.delete_old": "Удалять старые архивы перед упаковкой",
    "settings.manifest": "Включать _AUDAPACK_MANIFEST.json в архивы",
    "settings.excludes": "Исключения (по одному в строке):",
    "settings.runtime_dir_fmt": "Каталог данных пользователя: {path}",
    "settings.bridge_box": " Локальный мост AUDAPACK (порт 17843) ",
    "settings.autostart_box": " Автозапуск Windows (Scheduled Task) ",
    "settings.context_menu_box": " Контекстное меню Проводника ",
    "settings.widget_box": " Браузерный виджет AUDAPACK (Tampermonkey) ",
    "settings.br_start": "Запустить мост",
    "settings.br_stop": "Остановить мост",
    "settings.br_restart": "Перезапустить",
    "settings.br_copy_token": "Копировать токен",
    "settings.br_takeover": "Перехватить legacy-мост",
    "settings.br_status_checking": "Статус: ПРОВЕРКА...",
    "settings.br_status_running_fmt": "Статус: РАБОТАЕТ ({svc} v{v} / API {api} на порту {port})",
    "settings.br_status_legacy_fmt": "Статус: LEGACY РАБОТАЕТ (старый ACBBridge на порту {port})",
    "settings.br_status_stopped_fmt": "Статус: ОСТАНОВЛЕН (порт {port})",
    "settings.auto_install": "Установить автозапуск",
    "settings.auto_remove": "Удалить автозапуск",
    "settings.auto_repair": "Восстановить автозапуск",
    "settings.auto_status_checking": "Статус: ПРОВЕРКА...",
    "settings.auto_status_installed": "Статус: УСТАНОВЛЕН (задача 'AUDAPACK Bridge' стартует при входе)",
    "settings.auto_status_broken": "Статус: СЛОМАНО (путь в Scheduled Task не совпадает)",
    "settings.auto_status_none": "Статус: НЕ УСТАНОВЛЕН",
    "settings.ctx_install": "Установить",
    "settings.ctx_remove": "Удалить",
    "settings.ctx_status_installed": "Статус: УСТАНОВЛЕНО (в Проводнике)",
    "settings.ctx_status_none": "Статус: НЕ УСТАНОВЛЕНО",
    "settings.widget_status_fmt": "Скрипт: ГОТОВ (v{ver})",
    "settings.widget_install": "Установить / обновить в браузере",
    "settings.browser_select_title": "Выбор браузера",
    "settings.browser_select_prompt": "Выберите браузер для установки виджета AUDAPACK:",
    "settings.browser_browse": "Обзор...",
    "settings.browser_remember": "Запомнить выбор браузера",
    "settings.browser_running": "[Работает]",
    "settings.browser_opened_fmt": "Виджет открыт в {name}.",
    "settings.btn_open_install": "Открыть и установить",
    "settings.restore_defaults": "Восстановить проекты по умолчанию",
    "settings.restore_defaults_done": "Стандартный список проектов восстановлен.",
    "settings.restore_defaults_confirm": "Восстановить стандартный список проектов AUDAPACK?\nТекущий список будет заменён каноническими слотами.",
    "settings.save": "Сохранить настройки",
    "settings.close": "Закрыть",
    "settings.repair_all": "Восстановить всё",
    "settings.takeover_confirm": "Передать управление от legacy ACBBridge к AUDAPACK Bridge сейчас?\n\nЭто безопасно остановит legacy, запустит AUDAPACK Bridge и обновит Scheduled Task.",
    "settings.takeover_done": "AUDAPACK Bridge теперь активный.\nScheduled Task 'AUDAPACK Bridge' установлена.",
    "settings.takeover_fail_fmt": "Не удалось завершить перехват:\n{errs}",
    "settings.token_copied": "Секретный токен AUDAPACK Bridge скопирован в буфер.",
    "settings.repair_summary_fmt": "ВЕРХ: {ctx}\nМОСТ: {br}\nАВТО: {auto}",
    "settings.repair_ok": "OK",
    "settings.repair_failed": "СБОЙ",
    "settings.save_error": "Не удалось сохранить настройки.",

    # Errors
    "error.audit_not_ready": "Канонический файл ALL_3 не готов или не читается.",
    "error.copy_archive_failed_fmt": "Не удалось скопировать архив: {err}",
    "error.config_corrupt_fmt": "Повреждён файл конфигурации '{path}': {err}",
}

TRANSLATIONS_EN: dict[str, str] = {
    # Top toolbar
    "toolbar.pack_all": "PACK ALL ENABLED",
    "toolbar.refresh": "REFRESH AUDITS",
    "toolbar.paste_audit": "PASTE AUDIT (Ctrl+V)",
    "toolbar.open_output": "OPEN OUTPUT",
    "toolbar.settings": "SETTINGS & COMPONENTS",
    "toolbar.lang": "LANG",

    # Header row
    "header.slot": "SLOT",
    "header.project": "Project & Path",
    "header.wave": "WAVE",
    "header.temperature": "FRESHNESS",
    "header.handoff": "AUDIT",
    "header.pack": "PACK",
    "header.archive": "ARCHIVE",
    "header.menu": "···",

    # Archive freshness / pack progress (Qt delegate)
    "archive.fresh.tag": "  [fresh]",
    "archive.stale.tag": "  [stale]",
    "archive.old.tag": "  [old]",
    "archive.source_newer.tag": "  [SRC▲]",
    "pack.progress_fmt": "[PACK {pct}% {files}f {size}]",

    # Project row - checkbox / slot
    "row.missing_path": "[MISSING PATH]",
    "row.dirty_fmt": "[DIRTY {n}]",
    "row.clean": "[CLEAN]",
    "row.saipen": "[SAIPEN]",
    "row.empty_slot_fmt": "[ EMPTY SLOT {group} #{slot} ]",
    "row.add_project": "+ ADD PROJECT",
    "row.muted_badge": "[MUTED]",
    "row.ignore_tooltip": "Ignore / Dim currently (visual dimming, still packed)",

    # Audit column
    "audit.ready_all": "3/3",
    "audit.ready_partial_fmt": "{n}/3",
    "audit.ready_none": "0/3",

    # Copy audit button
    "btn.copy_audit": "AUDIT",
    "btn.copy_audit_done": "✓ AUDIT",
    "btn.copy_audit_disabled_hint": "AUDIT NOT READY",

    # Pack button
    "btn.pack": "PACK",
    "btn.pack_disabled_hint": "PATH MISSING",

    # Copy archive button (new)
    "btn.copy_archive": "ARCHIVE",
    "btn.copy_archive_done": "✓ ARCHIVE",
    "btn.copy_archive_no_archive": "NONE",
    "btn.copy_archive_disabled_hint": "NO ARCHIVE YET",

    # Menu
    "menu.move_up": "▲ Move Up",
    "menu.move_down": "▼ Move Down",
    "menu.move_to_group": "Move to Group...",
    "menu.move_to_group_fmt": "Move to {group}",
    "menu.move_dialog": "Move Slot / Group Dialog...",
    "menu.edit": "Edit Project...",
    "menu.delete": "Delete Project",
    "menu.paste_audit": "Paste Audit from Clipboard",
    "menu.mute_project": "Mute (visual only)",
    "menu.unmute_project": "Unmute",
    "menu.copy_archive": "Copy Archive to Clipboard",
    "menu.open_audit_dir": "Open Audit Folder",

    # Bottom status
    "status.ready": "Ready",
    "status.cancelling": "Cancelling...",
    "status.paste_audit_ok_fmt": "✓ Audit saved: {msg}",
    "status.paste_audit_fail_fmt": "Audit paste error: {err}",
    "status.packing_fmt": "[{i}/{n}] Packing {name}...",
    "status.pack_progress_fmt": "Packing: {files} files ({size}) | {file}",
    "status.pack_ok_fmt": "OK: {name} -> {files} files ({size})",
    "status.pack_fail_fmt": "FAIL: {name}: {err}",
    "status.pack_finished": "Packaging finished.",
    "status.pack_cancelled": "Packing cancelled by user.",
    "status.pack_starting": "Starting packaging...",
    "status.copied_audit_fmt": "Copied exact ALL_3 for {name} ({n} chars)",
    "status.copied_archive_fmt": "Archive copied to clipboard: {name}",
    "status.copy_archive_no_archive_fmt": "No archive found for {name}",
    "status.no_enabled_projects": "No enabled projects to pack.",
    "status.language_switched_fmt": "Language switched to {lang}",
    "status.group_projects_count_fmt": "({n} / {total} projects)",

    # Dialogs
    "dialog.add_title": "Add Project",
    "dialog.edit_title": "Edit Project",
    "dialog.field.display_name": "Display Name:",
    "dialog.field.source_path": "Source Path:",
    "dialog.field.priority_group": "Priority Group:",
    "dialog.field.audit_override": "Audit Override:",
    "dialog.browse": "Browse...",
    "dialog.browse_dir_title": "Select Project Directory",
    "dialog.save_add": "Add",
    "dialog.save_edit": "Save",
    "dialog.cancel": "Cancel",
    "dialog.confirm_remove_fmt": "Remove project '{name}' from slot {group} #{slot}?",

    # Settings dialog
    "settings.title": "AUDAPACK — Configuration & Components",
    "settings.tab.packing": "Packing & Auditing",
    "settings.tab.bridge": "Bridge & Autostart",
    "settings.output_dir": "Packing Output Directory (Default: Desktop/PACK):",
    "settings.audit_root": "Audit Root Directory (AUDITING_IMPLEMENTATION):",
    "settings.delete_old": "Delete old matching archives before packing project",
    "settings.manifest": "Include _AUDAPACK_MANIFEST.json in created archives",
    "settings.excludes": "Exclude Patterns (one per line):",
    "settings.runtime_dir_fmt": "User Runtime Directory: {path}",
    "settings.bridge_box": " AUDAPACK Loopback Bridge (Port 17843) ",
    "settings.autostart_box": " Windows Scheduled Task Autostart ",
    "settings.context_menu_box": " Windows Explorer Context Menu ",
    "settings.widget_box": " AUDAPACK Browser Widget (Tampermonkey) ",
    "settings.br_start": "Start Bridge",
    "settings.br_stop": "Stop Bridge",
    "settings.br_restart": "Restart",
    "settings.br_copy_token": "Copy Token",
    "settings.br_takeover": "Takeover Legacy Bridge",
    "settings.br_status_checking": "Status: CHECKING...",
    "settings.br_status_running_fmt": "Status: RUNNING ({svc} v{v} / API {api} on port {port})",
    "settings.br_status_legacy_fmt": "Status: LEGACY RUNNING (Old ACBBridge detected on port {port})",
    "settings.br_status_stopped_fmt": "Status: STOPPED (Port {port})",
    "settings.auto_install": "Install Autostart",
    "settings.auto_remove": "Remove Autostart",
    "settings.auto_repair": "Repair Autostart",
    "settings.auto_status_checking": "Status: CHECKING...",
    "settings.auto_status_installed": "Status: INSTALLED (Task 'AUDAPACK Bridge' starts at user logon)",
    "settings.auto_status_broken": "Status: BROKEN (Path in Scheduled Task does not match current repository)",
    "settings.auto_status_none": "Status: NOT INSTALLED (No logon Scheduled Task)",
    "settings.ctx_install": "Install",
    "settings.ctx_remove": "Remove",
    "settings.ctx_status_installed": "Status: INSTALLED (Active in Explorer)",
    "settings.ctx_status_none": "Status: NOT INSTALLED",
    "settings.widget_status_fmt": "Bundled Script: READY (v{ver})",
    "settings.widget_install": "Install / Update in Browser",
    "settings.browser_select_title": "Select Browser",
    "settings.browser_select_prompt": "Select browser to install AUDAPACK widget:",
    "settings.browser_browse": "Browse...",
    "settings.browser_remember": "Remember browser choice",
    "settings.browser_running": "[Running]",
    "settings.browser_opened_fmt": "Widget opened in {name}.",
    "settings.btn_open_install": "Open & Install",
    "settings.restore_defaults": "Restore Default Projects",
    "settings.restore_defaults_done": "Default project list restored.",
    "settings.restore_defaults_confirm": "Restore default AUDAPACK project list?\nCurrent list will be replaced with canonical slots.",
    "settings.save": "Save Settings",
    "settings.close": "Close",
    "settings.repair_all": "Repair All",
    "settings.takeover_confirm": "Transfer runtime ownership from legacy ACBBridge to AUDAPACK Bridge now?\n\nThis will safely stop legacy ACBBridge, start AUDAPACK Bridge, and update the logon Scheduled Task.",
    "settings.takeover_done": "AUDAPACK Bridge is now the active production bridge.\nScheduled Task 'AUDAPACK Bridge' installed.",
    "settings.takeover_fail_fmt": "Failed to complete takeover:\n{errs}",
    "settings.token_copied": "AUDAPACK Bridge secret token copied to clipboard.",
    "settings.repair_summary_fmt": "CTX: {ctx}\nBRIDGE: {br}\nAUTOSTART: {auto}",
    "settings.repair_ok": "OK",
    "settings.repair_failed": "Failed",
    "settings.save_error": "Failed to save configuration.",

    # Errors
    "error.audit_not_ready": "Canonical ALL_3 audit file is not ready or unreadable.",
    "error.copy_archive_failed_fmt": "Failed to copy archive: {err}",
    "error.config_corrupt_fmt": "Corrupted configuration file '{path}': {err}",
}

TRANSLATIONS: dict[str, dict[str, str]] = {
    "ru": TRANSLATIONS_RU,
    "en": TRANSLATIONS_EN,
}


_current_language: str = DEFAULT_LANGUAGE
_reload_callbacks: list[Callable[[str], None]] = []


def normalize_language(lang: Optional[str]) -> str:
    """Coerce user/system input to a supported language code, defaulting to ru."""
    if not lang:
        return DEFAULT_LANGUAGE
    cand = lang.strip().lower()
    if cand in TRANSLATIONS:
        return cand
    short = cand.split("-")[0].split("_")[0]
    if short in TRANSLATIONS:
        return short
    return DEFAULT_LANGUAGE


def set_language(lang: str) -> str:
    """Switch the active language. Returns the language actually applied."""
    global _current_language
    applied = normalize_language(lang)
    if applied != _current_language:
        _current_language = applied
        for cb in list(_reload_callbacks):
            try:
                cb(applied)
            except Exception:
                pass
    return applied


def get_language() -> str:
    return _current_language


def register_reload_callback(cb: Callable[[str], None]) -> None:
    """Register a callback fired when the language changes (receives new lang)."""
    if cb not in _reload_callbacks:
        _reload_callbacks.append(cb)


def unregister_reload_callback(cb: Callable[[str], None]) -> None:
    if cb in _reload_callbacks:
        _reload_callbacks.remove(cb)


def available_languages() -> tuple[str, ...]:
    return SUPPORTED_LANGUAGES


def t(key: str, **kwargs) -> str:
    """Translate ``key`` into the current language.

    Falls back: active lang -> en -> the key itself (so missing keys are visible
    in screenshots/tests instead of silently producing an empty label).
    ``kwargs`` are passed to ``str.format`` for placeholder substitution.
    """
    text = TRANSLATIONS.get(_current_language, {}).get(key)
    if text is None:
        text = TRANSLATIONS["en"].get(key, key)
    if kwargs:
        try:
            return text.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            return text
    return text


def language_display_name(code: str) -> str:
    """Human-readable name of a language code (used in the language switcher)."""
    names = {"ru": "RU", "en": "EN"}
    return names.get(code, code.upper())
