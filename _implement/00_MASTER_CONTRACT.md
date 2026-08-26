# AUDAPACK — MASTER IMPLEMENTATION CONTRACT

## Mission

Transform the existing lightweight Audit Folder Packer into **AUDAPACK**, a compact Windows desktop tool for:

1. clean project/file packaging;
2. priority-based project organization;
3. audit freshness/readiness tracking;
4. exact copying of latest ready audit handoff;
5. read-only SAIPEN-aware project intelligence;
6. Windows Explorer context-menu packaging;
7. managed browser-side AICHATBUTTONS-derived integration;
8. a single AUDAPACK-owned loopback bridge for audit delivery.

The result must remain a small utility, not a second SAIAUDIT, second FastPrompter, Electron app, database service, or giant dependency farm.

---

## Non-negotiable invariants

### 1. One project registry

Packing, audit routing, priority UI, SAIPEN awareness and bridge destination lookup must use one canonical project registry.

### 2. One packing engine

GUI packing, batch packing, silent mode and Explorer context menu must call the same engine.

### 3. One canonical audit storage engine

AUDAPACK owns the physical canonical audit files and canonical `__00_AUDIT_ALL_3.md`.

### 4. One component family

Browser widget and bridge are AUDAPACK components, not independent products requiring unrelated setup flows.

### 5. Copied state belongs to a content hash

`COPIED` means the exact current ALL_3 content was copied. A new ALL_3 hash automatically resets the state to `NEW`.

### 6. `.saipen` unlocks context, never mutation

AUDAPACK may read `.saipen` metadata and derive project-change context. It must not edit SAIPEN protocol state during normal packing.

### 7. Failure never masquerades as success

- invalid output root → no fake success;
- corrupt archive → no replacement;
- project mismatch → no mixed audit;
- failed bridge write → browser job remains retryable.

### 8. Small tool stays small

Prefer Python standard library + Tkinter. Do not add heavy frameworks without a demonstrated requirement.

---

## Canonical product terminology

User-facing product name:

`AUDAPACK`

Windows Explorer context action:

`Упаковать через AUDAPACK`

Accept old names only as migration/compatibility inputs:

- Audit Folder Packer
- AUPACK
- AICHATBUTTONS
- ACBBridge

New UI and documentation must not present them as competing product identities.

---

## Mandatory reference inspection

Before editing relevant subsystems, inspect the actual implementation, not only README prose.

### Current AUDAPACK

Preserve useful existing properties:

- Python stdlib;
- Tkinter;
- atomic config save;
- `.part` archive publication;
- ZIP verification;
- old ZIP deleted only after new ZIP verifies;
- unreadable/locked file skip behavior;
- background worker;
- cancel;
- silent `pythonw`;
- GUI hide-after-start behavior;
- GUI restore on failure;
- saved window size;
- current Golden Default styling.

### AICHATBUTTONS

Inspect:

- userscript architecture;
- prompt presets;
- audit presets;
- Auto3;
- project extraction;
- attachment detection;
- ownership/lease/fencing;
- stage state;
- recovery/continuation;
- durable browser queue;
- GM persistence;
- bridge requests;
- run/receipt identity;
- latest/history file logic;
- script installer/maintenance tooling;
- tests.

Read the latest ALL_3 audit for AI ChatButtons before porting code. Capability may be migrated; known defects must not be.

### AUDITING_IMPLEMENTATION

Treat these as canonical priority groups:

- `MAIN0`
- `MAIN1`
- `SIDE0`
- `SIDE1`

Each has exactly 6 visible slots.

Canonical per-project audit files:

- `<Project>__01_AUDIT_CORE.md`
- `<Project>__02_AUDIT_SECOND_WAVE.md`
- `<Project>__03_AUDIT_PERFORMANCE.md`
- `<Project>__00_AUDIT_ALL_3.md`
- `_history\...`

### FastPrompter / SAIPENVIEW

Use as visual-density and hierarchy references only. Do not inherit their heavy dependencies merely to imitate appearance.

### SAIAUDIT

Use as a reference for:

- component management;
- owner/lifecycle boundaries;
- health state;
- bridge durability;
- atomic publication;
- explicit failure states.

Do not turn AUDAPACK into SAIAUDIT.

---

## Global coding policy

- Internal identifiers/code: English.
- Keep source modular.
- Do not keep adding thousands of lines to one `pack_all_audit_gui.py`.
- Separate at least: packing, projects/config, audit indexing, bridge, UI.
- Prefer zero new runtime dependencies for v1.
- No database.
- No web dashboard.
- No Electron.
- No Docker.
- No bundled Chromium.
- No mandatory Node runtime.

Reasonable target structure:

```text
AUDAPACK/
    AUDAPACK.pyw
    AUDAPACK.vbs
    START_AUDAPACK.cmd

    audapack/
        __init__.py
        app.py
        config.py
        models.py
        packing.py
        projects.py
        audits.py
        saipen.py
        context_menu.py

        bridge/
            __init__.py
            server.py
            state.py
            storage.py
            lifecycle.py

        components/
            __init__.py
            manager.py
            widget.py

        ui/
            __init__.py
            theme.py
            main_window.py
            dialogs.py
            project_rows.py
            settings.py

    resources/
        AUDAPACK_WIDGET.user.js

    tests/
    UI.md
    README.md
```

This is a guide, not a mandatory exact tree.

---

## Persistent vs computed state

Persist:

- project registry;
- slot assignments;
- paths;
- excludes;
- packing settings;
- bridge preferences;
- copied ALL hash/time;
- window state.

Compute:

- audit age;
- audit temperature;
- filesystem existence;
- SAIPEN marker;
- Git dirty status;
- bridge health;
- ALL readiness.

Do not serialize transient UI labels.

---


---

## Canonical AUDAPACK source/runtime ownership (v2 clarification)

The intended working source root on the user's machine is:

```text
V:\___VAC\__K\__CODE\_PY\_AUDAPACK
```

Treat any bundled/copied `_AICHATBUTTONS` tree inside AUDAPACK as a **read-only reference snapshot** only. It is allowed to exist for comparison and porting, including its `.git`, tests and legacy `ACBBridge`, but it must never become a production runtime dependency. Exclude that reference tree from normal release/package output unless the user explicitly asks to include development references.

Production ownership after migration must be:

```text
V:\___VAC\__K\__CODE\_PY\_AUDAPACK
    -> AUDAPACK desktop source/runtime entry points
    -> bundled AUDAPACK Widget source
    -> AUDAPACK Bridge source

%LOCALAPPDATA%\AUDAPACK
    -> mutable local bridge state
    -> token
    -> logs
    -> migration backup/markers
```

Do not keep `%LOCALAPPDATA%\ACBBridge\app` as the active code copy after takeover.

### Legacy installation that MUST be detected and migrated

The existing legacy AICHATBUTTONS bridge installer uses:

```text
Source reference:
V:\___VAC\__K\__CODE\_TAMPERMONKEY\_AICHATBUTTONS\ACBBridge\INSTALL.cmd

Scheduled Task:
ACBBridge

Legacy local root:
%LOCALAPPDATA%\ACBBridge

Legacy copied app:
%LOCALAPPDATA%\ACBBridge\app\acbbridge.py

Legacy default port:
127.0.0.1:17843
```

The new implementation must perform a controlled takeover rather than merely installing another server beside it.

New identity:

```text
Component: AUDAPACK Bridge
Scheduled Task: AUDAPACK Bridge
Canonical source root: V:\___VAC\__K\__CODE\_PY\_AUDAPACK
Local mutable state: %LOCALAPPDATA%\AUDAPACK
```

Migration must preserve compatible user settings/token where safe, verify ownership before stopping/killing a process, remove/disable the old Scheduled Task only after the new bridge is verified healthy, and leave a recoverable migration record.

### Browser-to-disk routing authority

The Tampermonkey widget must not own Windows filesystem paths. It may query the bridge for a sanitized logical project registry snapshot:

```text
project_id
project display name
priority group
slot
enabled/audit-enabled state
```

It must not need the physical audit root.

Canonical routing belongs to AUDAPACK Bridge:

```text
Widget detects/completes audit
    -> Widget resolves/matches logical project against registry snapshot
    -> Widget sends project_id + run/wave/receipt/content
    -> Bridge re-validates project_id against canonical registry
    -> Bridge computes destination
    -> AUDITING_IMPLEMENTATION\<MAIN0|MAIN1|SIDE0|SIDE1>\<Project>
    -> canonical wave files
    -> canonical ALL_3 after valid 3/3
```

The bridge always re-validates routing. Browser-provided group, slot or path can be treated as informational only and must never authorize a destination.

## Required final user flows

### GUI package

```text
Open AUDAPACK
→ locate project
→ PACK
→ verified ZIP
```

### Explorer package

```text
Right click project/file
→ Упаковать через AUDAPACK
→ verified ZIP
```

### Audit handoff

```text
Open AUDAPACK
→ project row says HOT/WARM/etc + ALL
→ КОПИРОВАТЬ АУДИТ
→ exact ALL_3 in clipboard
→ ✓ COPIED
```

### New ALL arrives

```text
new ALL_3 hash
→ old copied state invalid
→ NEW
→ COPY enabled again
```

### Browser flow

```text
AUDAPACK Widget
→ durable browser queue
→ AUDAPACK Bridge
→ project registry routing
→ priority/project audit folder
→ atomic wave write
→ canonical ALL_3 when 3/3
→ GUI refresh
```

---

## Wave discipline

Every wave must:

1. inspect current implementation;
2. inspect relevant reference project;
3. identify state owner;
4. make the smallest coherent design;
5. implement;
6. add/adjust tests;
7. run tests;
8. leave app runnable;
9. produce a wave handoff.

Do not begin the next wave until the current wave's gate passes.

Do not ask the user for routine implementation choices that can be safely derived from the existing project.

---

## Required wave handoff format

At the end of every wave return:

```text
AUDAPACK WAVE <X>: COMPLETE / PARTIAL / BLOCKED

SCOPE COMPLETED:
- ...

FILES CHANGED:
- ...

MIGRATIONS:
- ...

TESTS:
- <exact command>
- <exact result>

MANUAL SMOKE:
- ...

REGRESSIONS CHECKED:
- ...

OPEN RISKS:
- ...

NEXT WAVE READY:
YES / NO
```

Never invent PASS results.
