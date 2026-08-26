# WAVE B — PROJECT REGISTRY, PRIORITY GROUPS, SIX-SLOT UI

## Goal

Replace the generic folder list as the main mental model with a canonical project room:

- MAIN0
- MAIN1
- SIDE0
- SIDE1

Each group has exactly 6 visible slots.

---

## Required work

### B1. Canonical registry

Implement one canonical project registry used by future subsystems.

Rules:

- one project occupies at most one slot;
- empty slot is valid;
- exactly four priority groups;
- exactly six slots in each;
- order is fixed: MAIN0, MAIN1, SIDE0, SIDE1.

### B2. Import current AUDITING_IMPLEMENTATION

Scan configured audit root.

Expected existing structure includes projects already under MAIN0 / MAIN1 / SIDE0.

Import those projects into matching slots where possible.

Do not silently rename or move existing audit directories.

Create/recognize SIDE1 if absent.

Unfilled positions remain explicit empty slots.

### B3. Merge legacy pack folders into registry

Old `repos` entries must not disappear.

If a source path has no audit-folder match, import it as an unassigned or first-safe empty slot according to a deterministic migration policy.

Do not create duplicates by path.

### B4. Main UI redesign

Primary screen should visually show all groups in one scrollable project room.

Do not use four tabs unless unavoidable.

Each group:

- clear header;
- visually distinct but within Golden Default palette;
- six rows;
- fixed slot number.

Example concept:

```text
MAIN0
  1 FastPrompter       [PACK]
  2 SAIAUDIT           [PACK]
  3 ...
  4 ...
  5 ...
  6 ...

MAIN1
  ...

SIDE0
  ...

SIDE1
  1 [ EMPTY SLOT ]     [ADD PROJECT]
  ...
```

### B5. Project row baseline

At this wave, each populated row needs at least:

- enabled toggle;
- project name;
- path status;
- slot/group;
- PACK action;
- edit/remove/move action;
- placeholder area for future audit/SAIPEN badges.

Do not add fake audit status yet.

### B6. Move project

Provide deterministic controls to move a project between groups/slots.

Drag-and-drop is optional.

Buttons/context actions must exist even if drag-and-drop exists.

Moving registry assignment must not silently move historical audit files.

If you add a "move audit folder too" operation, it must be explicit.

### B7. Missing project paths

Display missing source paths clearly.

Missing path must not crash startup or erase the registry entry.

### B8. Main actions

Add/retain:

- PACK ENABLED;
- PACK SELECTED;
- per-row PACK;
- Open Output;
- Settings.

All must call the same pack engine.

---

## Tests

Required:

1. four groups exist;
2. each has six slots;
3. project cannot occupy two slots;
4. empty slots persist;
5. old `repos` migrate;
6. current AUDITING_IMPLEMENTATION folders import;
7. move project;
8. remove frees slot;
9. duplicate path prevented;
10. missing path handled;
11. restart preserves registry.

---

## Acceptance gate

Wave B passes only if the user can open AUDAPACK and immediately see all 24 slots grouped into MAIN0/MAIN1/SIDE0/SIDE1, with existing projects migrated and packaging still working.

Do not begin Wave C until the registry is stable, because later audit routing depends on it.
