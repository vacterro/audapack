# AUDAPACK [![Version](https://img.shields.io/badge/версия-0.1.0-gold.svg)](CHANGELOG.md)

<p align="center">
  <b><a href="README.md">English</a></b> • <b><a href="README.ru.md">Русский</a></b>
</p>

<p align="center">
  <img src="resources/app_icon.png" width="128" height="128" alt="Логотип AUDAPACK">
</p>

**AUDAPACK** — быстрый десктопный диспетчер проектов, аудит-рум и мост автоматизации браузера для Windows.

<p align="center">
  <img src="resources/screenshot.png" alt="Скриншот интерфейса AUDAPACK" width="800">
</p>

Объединяет в себе:
1. **Чистую упаковку проектов** (проверенные ZIP архивы с `.part` стейджингом, фильтрацией исключений и манифестом);
2. **Приоритетную комнату проектов** (24 слота в группах `MAIN0`, `MAIN1`, `SIDE0`, `SIDE1` по 6 слотов);
3. **Отслеживание свежести и готовности аудитов** (`0/3`, `1/3`, `2/3`, `3/3`, `HOT`, `WARM`, `COOL`, `COLD`, `STALE`);
4. **Копирование аудита в 1 клик** (копирует каноничный `__00_AUDIT_ALL_3.md`, отслеживает SHA-256 хэш, сбрасывает статус на `NEW` при поступлении нового аудита);
5. **Интеграцию с SAIPEN** (распознаёт `.saipen`, отображает статус Git и задачи, формирует `_AUDAPACK_MANIFEST.json`);
6. **Контекстное меню Проводника** (`Упаковать через AUDAPACK` для папок и файлов);
7. **Браузерный виджет** (`resources/AUDAPACK_WIDGET.user.js` для Tampermonkey с цепочкой Auto3);
8. **Локальный мост AUDAPACK** (HTTP демон на `127.0.0.1:17843` с токен-авторизацией и изоляцией runId).

---

## Быстрый старт

### 1. Запуск интерфейса
Двойной клик по `AUDAPACK.vbs` (тихий запуск без консоли) или:
```cmd
pythonw AUDAPACK.pyw
```

### 2. Тихая упаковка
Двойной клик по `PACK_ALL_SILENT.vbs` или:
```cmd
pythonw AUDAPACK.pyw --silent
```

### 3. Меню Проводника Windows
Установите интеграцию из окна **Настройки** или командой:
```cmd
python AUDAPACK.pyw --install-context-menu
```

---

## Документация и Wiki

Подробные руководства доступны в каталоге [`docs/wiki/`](docs/wiki/):
- [Главная Wiki](docs/wiki/Home.md)
- [Архитектура и демон моста](docs/wiki/Architecture-and-Bridge.md)
- [Автоматизация Auto3](docs/wiki/Auto3-Audit-Pipeline.md)
- [Стиль Golden Vintage](docs/wiki/UI-Golden-Vintage.md)
- [Консольный запуск и упаковка](docs/wiki/CLI-and-Silent-Packaging.md)
