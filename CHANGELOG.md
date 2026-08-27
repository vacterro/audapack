# Changelog

All notable changes to this project will be documented in this file.

## [0.1.1] - 2026-08-27

### Added
- Wave N Qt production cutover: Qt (PySide6) now default launcher (`--ui qt`), Tkinter kept as `--ui tkinter` fallback.

### Changed
- Performance: AuditIndexer batch index + dir cache (cached scans 60→2ms, missing 349→6ms, scan_all 308→130ms), lazy Qt model startup (0ms visible), registry O(1) id index.

## [0.1.0] - 2026-08-27

### Added
- Complete AUDAPACK desktop suite (Tkinter & PySide6 Qt support).
- Real-time audit room management across 24 slots (MAIN0, MAIN1, SIDE0, SIDE1).
- Audit freshness indicator with color-coded status (HOT, WARM, COOL, COLD, STALE).
- Archive creation with .part staging, atomic commit, and CRC integrity check.
- Handshake and auto-ingest HTTP bridge for Tampermonkey userscript.
- Comprehensive test suites (162 Python tests, 86 Widget tests).
- Integrated complete developer documentation wiki (`docs/wiki/`).
- Added full Russian documentation (`README.ru.md`) and language switcher.

### Changed
- Streamlined UI headers and action buttons (ВОЛНА, СВЕЖЕСТЬ, АУДИТ, СБОРКА, АРХИВ).
- Enhanced Tampermonkey widget auto-send with pointer/mouse dispatch and A3 state preservation.
- Strict runId boundary isolation preventing stale audit wave badge display.
- Optimized scan and regex engines across audit and packing subsystems.
