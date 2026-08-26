# WAVE A — BASELINE, REBRAND, MODULARIZATION, CONFIG MIGRATION

## Goal

Create a clean AUDAPACK baseline without changing the product's core packing behavior.

This wave is intentionally boring. That is a feature. A later feature wave built on a broken baseline is just a more expensive bug.

---

## Required work

### A1. Establish baseline

Before changing source:

- run current GUI;
- run current silent mode;
- pack a representative small test folder;
- record current archive naming;
- record current excludes;
- verify current `.part` / ZIP verification behavior;
- verify config load/save;
- inspect old launchers.

Create baseline tests where absent.

### A2. Rebrand to AUDAPACK

Change user-facing identity:

- window title;
- README;
- launcher naming where safe;
- logs;
- settings labels.

Keep compatibility wrappers for old launchers if useful.

Canonical entry points should converge toward:

```text
AUDAPACK.pyw
AUDAPACK.vbs
START_AUDAPACK.cmd
```

Old `PACK_ALL_SILENT.vbs` may remain as a thin redirect.

### A3. Split the monolith

Extract current logic into modules without rewriting algorithms unnecessarily.

Minimum separation:

- config/models;
- packing;
- UI;
- application entry.

No bridge/browser work in this wave.

### A4. Add config schema version

Current legacy config likely contains fields such as:

```text
repos
excludes
output_dir
delete_old
window_size
```

Create a versioned schema.

Migration must:

1. load old JSON;
2. validate;
3. normalize;
4. convert;
5. write atomically;
6. preserve a valid original if conversion fails.

Never replace a malformed/non-readable existing config with a blank default without preserving evidence.

### A5. Preserve packing invariants

Do not regress:

- `.part` creation;
- cancel cleanup;
- Zip64;
- timestamp-safe names;
- archive verification;
- previous archive survival if new package fails;
- cleanup of old archive only after success;
- skip-and-log unreadable files;
- background worker;
- no console flash in silent mode;
- window restore on errors.

### A6. Centralize theme tokens

Move Golden Default tokens into one theme module if not already centralized.

Do not redesign UI yet.

### A7. Introduce basic models

Add a minimal `Project` model, even if the old UI still uses the legacy list temporarily.

Expected logical fields eventually include:

```text
id
display_name
source_path
enabled
priority_group
slot
archive_name
audit_project_name
last_copied_audit_hash
last_copied_at
```

Do not force all future fields into config if they are computed.

---

## Tests

At minimum:

- config legacy migration;
- config atomic save;
- pack folder;
- excludes;
- unreadable file;
- cancel;
- `.part` cleanup;
- ZIP verify failure preserves previous archive;
- Unicode path;
- path with spaces.

---

## Acceptance gate

Wave A passes only if:

- app launches;
- old config migrates;
- current folders are not lost;
- GUI packaging works;
- silent packaging works;
- archive safety behavior is preserved;
- source is modular enough for later waves;
- tests are green.

Do not begin Wave B if the packer baseline is unstable.
