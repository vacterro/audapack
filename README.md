# AUDAPACK [![Version](https://img.shields.io/badge/version-0.1.0-gold.svg)](CHANGELOG.md)

<p align="center">
  <img src="resources/app_icon.png" width="128" height="128" alt="AUDAPACK Logo">
</p>

**AUDAPACK** is a lightweight Windows desktop utility and audit room controller for software projects.

<p align="center">
  <img src="resources/screenshot.png" alt="AUDAPACK UI Screenshot" width="800">
</p>

It unifies:
1. **Clean project packaging** (robust, timestamped, verified ZIP archives with `.part` staging and optional manifest);
2. **Priority project room** (24 slots across `MAIN0`, `MAIN1`, `SIDE0`, `SIDE1` with 6 slots each);
3. **Audit freshness & readiness tracking** (`0/3`, `1/3`, `2/3`, `3/3`, `ALL`, and `HOT` / `WARM` / `COOL` / `COLD` / `STALE` temperatures);
4. **Exact 1-click audit handoff copying** (copies canonical `__00_AUDIT_ALL_3.md`, tracks SHA-256 hash, switches to `✓ COPIED`, automatically resets to `NEW` when a fresh audit arrives);
5. **Read-only SAIPEN awareness** (detects root `.saipen`, displays current task/phase/Git dirty status, generates `_AUDAPACK_MANIFEST.json` inside archives without modifying protocol state);
6. **Windows Explorer context menu** (`Упаковать через AUDAPACK` for folders and single files via HKCU);
7. **Bundled browser widget** (`resources/AUDAPACK_WIDGET.user.js` for Tampermonkey with Auto3 audit automation);
8. **AUDAPACK loopback bridge** (HTTP daemon on `127.0.0.1:17843` with token authentication, per-run transactions, receipt idempotency, collision-resistant history, and canonical ALL_3 generation).

Zero heavy frameworks: Built entirely on Python 3 standard library + Tkinter.

---

## Quick Start

### 1. Launch GUI
Double-click `AUDAPACK.vbs` (silent, no console) or run:
```cmd
pythonw AUDAPACK.pyw
```

### 2. Silent Packing
Double-click `PACK_ALL_SILENT.vbs` or run:
```cmd
pythonw AUDAPACK.pyw --silent
```

### 3. Explorer Context Menu
Install the context menu from **Settings & Components** inside the GUI, or run:
```cmd
python AUDAPACK.pyw --install-context-menu
```
Now right-click any folder or file in Windows Explorer and select **Упаковать через AUDAPACK**.

---

## Architecture & Priority Room

The main screen presents a scrollable 24-slot project room organized into four canonical priority groups:
- **MAIN0**: Primary active projects (Slots 1–6)
- **MAIN1**: Secondary active projects (Slots 1–6)
- **SIDE0**: Utility / supporting projects (Slots 1–6)
- **SIDE1**: Long-term / reserve projects (Slots 1–6)

Each slot shows:
- Enable/disable checkbox
- Slot number (`#1`..`#6`)
- Project name and path status (`[MISSING PATH]` warning if path moved)
- `[SAIPEN]` badge and Git status (`[CLEAN]` or `[DIRTY N]`)
- Audit readiness (`0/3`, `1/3`, `2/3`, `3/3`, `ALL`)
- Audit temperature (`HOT · 2h 14m`, `WARM · 18h`, `COOL`, `COLD`, `STALE`, `NONE`)
- `КОПИРОВАТЬ АУДИТ` button (`✓ COPIED` when up-to-date, `NEW` when fresh audit is available)
- `PACK` button for instant archive creation
- Slot action menu (Edit, Move, Delete)

---

## Audit Temperature & Copying

Temperature thresholds (calculated from `GENERATED_AT` or `DATE_TIME` metadata in audit markdown):
- **HOT**: 0–6 hours
- **WARM**: >6–24 hours
- **COOL**: >24–72 hours
- **COLD**: >72 hours–7 days
- **STALE**: >7 days
- **NONE**: No audit exists

When clicking **КОПИРОВАТЬ АУДИТ**:
1. Exact canonical `__00_AUDIT_ALL_3.md` content is copied to the clipboard.
2. The SHA-256 hash of the content is saved in project state.
3. The button displays `✓ COPIED`.
4. When a new audit is delivered (or modified) with a different hash, the state automatically flips back to `NEW`.

---

## Browser Integration & Bridge

AUDAPACK includes a bundled Tampermonkey userscript: `resources/AUDAPACK_WIDGET.user.js`.

### Flow:
1. Browser widget automates audit waves (Auto3: Core, Second Wave, Performance).
2. Completed waves are enqueued in durable browser storage (`GM_setValue`).
3. The widget flushes waves to the AUDAPACK Bridge at `http://127.0.0.1:17843/v1/audits`.
4. The bridge resolves destination folders via the canonical Project Registry (`MAIN0/`, `MAIN1/`, etc.).
5. Waves are written atomically. When 3/3 waves are complete, canonical `__00_AUDIT_ALL_3.md` is generated.
6. The GUI is notified in real time and updates the audit cockpit.

---

## CLI Reference

```text
usage: AUDAPACK.pyw [-h] [--pack PATH] [--pack-project ID] [--silent]
                    [--install-context-menu] [--remove-context-menu]
                    [--status] [--bridge]

options:
  --pack PATH             Pack specified directory or file into archive
  --pack-project ID       Pack project by ID
  --silent                Pack all enabled projects silently without UI
  --install-context-menu  Install Explorer context menu entry
  --remove-context-menu   Remove Explorer context menu entry
  --status                Print registry and audit status to stdout
  --bridge                Run AUDAPACK bridge server in foreground
```

---

## Invariants & Safety

- **Archive safety**: Archives are written to `.part` files first, verified with `testzip()`, and only then replace target archives.
- **Fail-closed bridge**: Requests are restricted to loopback (`127.0.0.1`), authenticated with a high-entropy secret token, and payload-size bounded.
- **Read-only SAIPEN**: Normal AUDAPACK operations never write to or modify `.saipen` files.
