# Changelog

All notable changes to this project will be documented in this file.

## [0.2.1] - 2026-08-31

### Added
- Widget BLOCKED transparency: sticky banner with exact reason (clean-state-lost, canonical-start-rejected, bridge-marked-blocked) and numbered next-steps per failure class.
- Widget Clear log button in Bridge diagnostics header wipes the `BRIDGE_DIAGNOSTIC_LOG_KEY` and starts fresh.
- Qt tray toast now carries the job error string for BLOCKED/FAILED notifications.
- Widget regression suite w4-007-browser-worker-blocked.test.js (4 tests for the blocked-message formatter).

## [0.2.0] - 2026-08-31

### Added
- Durable filesystem-backed INAUDIT Inbox with authenticated Bridge API, atomic capture/assignment, recovery, archive, duplicate detection, explainable project classification, aliases, and conversation affinity.
- One-click `IA` response/block capture with exact Markdown preservation and a bounded idempotent IndexedDB offline spool.
- Qt INAUDIT Inbox/Layers interface, assignment actions, project counters, clipboard `IA+`, and source provenance.
- Dedicated AUDAPACK Chromium profile launcher and installer using Chrome/Cent/Edge/Vivaldi/Opera compatibility with background-throttling protections.

### Fixed
- Browser workers no longer require Brave; clean root ChatGPT tabs in supported Chromium browsers can claim audits while occupied tabs remain fail-closed.
- Windows config/token persistence tolerates brief sharing violations while still surfacing persistent I/O failures.

## [0.1.3] - 2026-08-30

### Fixed
- Project Room tree: single click on a project slot only selects it; double click is now required to open the Instances manager. Previously a single click opened the manager unexpectedly.

## [0.1.2] - 2026-08-27

### Added
- Generic Quick3/Super10 audit campaign profiles with dynamic wave progression.
- Qt project room with archive freshness, pack progress, drag-and-drop, and targeted updates.
- Tampermonkey recovery, fresh-archive START flow, and terminal-state regression coverage.

### Fixed
- T-13 fs-safe reconciliation: legacy raw-named artifact paths now resolve via sanitized name (sanitize_project_name) with pytest regression.
- Audit ingest now rolls back wave, canonical, and live campaign files on write failure and reports persistence errors honestly.
- Stale or hidden Continue generating controls no longer prevent acceptance of a structurally complete audit wave.

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
