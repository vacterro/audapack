# Changelog

All notable changes to this project will be documented in this file.

## [0.3.0] - 2026-09-05

### Added
- Audit fidelity profiles. Packing now runs under one of four declared profiles — COMPACT, STANDARD (default), DEEP, FULL — each with a soft byte budget (10 / 30 / 100 MB, uncapped) and per-directory media sampling. An archive is now explicitly either an *audit representation* (COMPACT/STANDARD/DEEP) or a *full snapshot* (FULL), and the manifest says which: `fidelity_profile`, `archive_semantics`, `budget_bytes`, `budget_met`, per-reason exclusion totals, `largest_omitted`, `pruned_directories` and a `media_inventory` that reports every media group it saw — exact file counts and byte totals per directory and media class, plus a bounded sample of the kept and omitted filenames. Profile, byte cap, sample counts and `always_include`/`always_exclude` overrides are configurable (`config.example.json`, Settings dialog).
- The budget is deliberately soft: mandatory audit material (code, tests, configs, manifests, schemas, build/runtime-referenced assets and anything named in `always_include`) is never trimmed to reach a target. A source-heavy project overshoots its profile budget and the manifest declares `budget_met: false` rather than silently dropping source.
- INAUDIT rename and routing actions. Layers renumbers an audit layer through an exclusive create that never overwrites an existing `audit/N.md` and refuses a number already taken; Inbox renames a capture's title in place. A capture can be routed before assignment: **Pin project** records the operator's own target and outranks the classifier's guess everywhere it is displayed, and **Assign** refuses an unregistered project or an already assigned capture instead of writing twice.
- Double-clicking a project row in the Project Room opens that project's INAUDIT inbox, the same surface the `IA` badge counts.

### Fixed
- Packing accounting no longer goes quietly false on an unusual tree. The identity `discovered == included + excluded + failed` now holds across symlinked files, unreadable entries and untraversable directories, and a partly enumerated tree is declared with `walk_incomplete` instead of being presented as complete accounting. The symlink probe is itself a `stat`, so before this an EACCES file escaped the per-file guard, ended the whole walk, and reported an empty tree as a valid audit representation.
- Bridge status endpoints stop re-deriving per-request state: the agent-state stamp uses a request-local `(root, binding)` memo, `invalid_auth` compaction runs under canonical proof with byte-identical token-replacement recovery, index writes are membership-only, and job pruning keeps sole copies under a 400-job bound.
- The Project Room `IA` badge now tells the truth about pending captures. Three defects stacked: the INAUDIT snapshot was rebuilt before the project dictionary it iterates was repopulated, so a fresh room snapshotted nothing and every badge read 0 until the operator opened an inbox by hand; the pending count read only the classifier's suggestion, hiding every hand-pinned capture; and the delegate drew the badge only for a non-zero count, so the surface that was supposed to report the queue disappeared exactly when it was empty and gave no way to tell "no captures" from "not computed".

## [0.2.3] - 2026-09-01

### Fixed
- Launching a Chromium worker no longer flashes a black console window that steals focus: `detect_installed_browsers()` ran bare `powershell` on every launch, and each windowless AUDAPACK process (pythonw GUI, Bridge daemon) made Windows allocate a new console. All console-tool spawns (powershell, schtasks, wmic, taskkill, git, browser Popen) now go through `audapack/procutil.py` (`CREATE_NO_WINDOW` + `STARTF_USESHOWWINDOW/SW_HIDE`), extending the P0-1 autostart pattern to the whole worker-launch chain.
- The Bridge no longer spawns a process storm behind its own status endpoints: `/health` and `/v1/status` recomputed the git build identity on every request (two `git` spawns each, four per GUI poll cycle, roughly twice a second while an audit is unsettled) and re-hashed the 800 KB widget bundle alongside it. Build identity is now computed once per process and the bundle digest is cached on (mtime, size). This was the actual source of the endless flashing windows during an audit; hiding the console only hid the symptom.
- Managed Chromium workers no longer land or work inside ChatGPT's new **Work** surface (widget 0.0.28): the Work composer ("Work on anything") is detected, `clean_for_audit` is refused while it is active (`worker-in-work-mode` claim block), and the 30 s housekeeping watchdog clicks the exact Chat control to return the window to the normal chat on its own, logging `worker_work_mode_switched` / `worker_work_mode_blocked` diagnostics. A window owning a dispatch is never touched.

## [0.2.2] - 2026-08-31

### Fixed
- `START AUDIT` now owns the complete zero-worker flow: it launches the dedicated Chromium worker, keeps the durable dispatch queued, and never asks for a second click.
- Worker-capacity preparation no longer mutates Qt status UI from a background thread; busy and launch-failed states return to the GUI-thread completion callback.
- Project Room uses consistent `START AUDIT` terminology for selection, queue, worker, and failure messages.

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
