# WAVE E — WINDOWS CONTEXT MENU, SINGLE-FILE PACKING, CLI/SILENT UNIFICATION

## Goal

Make packaging genuinely one-click from Windows Explorer while preserving one engine and no-console behavior.

---

## Required work

### E1. Canonical command surface

Add explicit command modes as appropriate:

```text
--pack <path>
--pack-project <id>
--silent
--install-context-menu
--remove-context-menu
--status
```

Exact parser structure is flexible.

All routes must call the canonical packing engine.

### E2. Single file support

AUDAPACK accepts:

- folder;
- single file.

Single file archive contains the file without manufacturing an absurd absolute-path directory tree.

### E3. Per-user Explorer context menu

Install via HKCU, not admin-wide HKLM.

User-facing action:

`Упаковать через AUDAPACK`

Support at minimum:

- directories;
- files, if single-file packaging is implemented.

Use safe quoting for:

- spaces;
- Cyrillic;
- parentheses;
- ampersands;
- common Windows path edge cases.

### E4. GUI component management

Settings/GUI must expose:

```text
Context Menu
Status: INSTALLED / NOT INSTALLED
[ Install ]
[ Remove ]
```

Install/remove must be idempotent.

No Regedit required.

### E5. Silent context workflow

Explorer action should not launch a console window.

Default:

```text
right click
→ AUDAPACK background pack
→ archive published
```

If failure occurs, preserve diagnostics in log and optionally a compact error dialog.

Do not open the full main window on every context-menu package.

### E6. Old launchers

Old scripts may remain as compatibility wrappers but must not contain independent packing logic.

### E7. Excludes

Normalize current excludes into a reusable packing profile.

Do not treat `.saipen` as junk.

Do not silently change `.git` inclusion behavior. If policy changes, make it explicit and migration-safe.

---

## Tests / smoke

Automated where possible:

- folder package through CLI;
- single-file package;
- Unicode;
- spaces;
- ampersand path;
- same core engine invoked.

Windows smoke:

1. install context menu;
2. install twice;
3. folder action;
4. file action;
5. Cyrillic path;
6. remove;
7. remove twice;
8. no admin prompt;
9. no console flash;
10. archive verifies.

---

## Acceptance gate

Wave E passes only if right-click packaging works reliably and shares the exact same engine as GUI packing.
