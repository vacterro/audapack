# WAVE D — SAIPEN-AWARE READ-ONLY PROJECT INTELLIGENCE

## Goal

If a project's root contains `.saipen`, AUDAPACK unlocks useful read-only context without becoming a SAIPEN protocol writer.

---

## Detection rule

SAIPEN marker:

```text
<project_root>\.saipen
```

Do not search arbitrary nested vendor/submodule trees for `.saipen`.

---

## Required work

### D1. SAIPEN badge

Project row shows a clear `SAIPEN` badge when root marker exists.

Missing/malformed SAIPEN metadata must never block packaging.

### D2. Read-only metadata summary

Where available, read useful compact fields from:

- `.saipen\STATE.md`
- `.saipen\BOARD.md`
- `.saipen\IDENTITY.md`

Extract only what can be reliably parsed, for example:

```text
task
phase
next_action
updated
```

Show compactly in details/tooltip/panel.

Do not flood every project row with long protocol text.

### D3. Never mutate `.saipen`

Normal AUDAPACK operations must not:

- edit STATE;
- edit BOARD;
- edit DONE;
- edit INBOX;
- add tickets;
- transition phases;
- "repair" SAIPEN.

Add tests that detect accidental writes.

### D4. Project-change awareness

For SAIPEN projects, determine whether project content changed since last successful package/snapshot.

Preferred evidence:

#### If Git exists

Read-only:

```text
HEAD
branch
dirty yes/no
changed tracked count
untracked count
```

Use bounded Git commands.

Do not crawl `.git/objects`.

#### If no Git

Maintain a lightweight AUDAPACK snapshot:

```text
relative path
size
mtime_ns
```

Hash only candidates where needed.

Store snapshot outside the project or in AUDAPACK-owned config/state, not in `.saipen`.

### D5. Display change status

Example:

```text
SAIPEN · DIRTY · 4 files
```

or:

```text
SAIPEN · CLEAN
```

This is advisory context, not a packing blocker.

### D6. Full archive remains default

Do not switch SAIPEN projects to incremental/partial ZIPs by default.

Audit consumers need a self-contained project.

### D7. Add package manifest

Add an AUDAPACK-owned metadata entry into the archive, e.g.:

`_AUDAPACK_MANIFEST.json`

Possible fields:

```json
{
  "schema_version": 1,
  "product": "AUDAPACK",
  "created_at": "...",
  "project": "...",
  "source_kind": "folder",
  "saipen_detected": true,
  "git": {
    "available": true,
    "branch": "main",
    "head": "...",
    "dirty": true
  },
  "files_added": 123,
  "files_skipped": 2
}
```

Do not include secrets or entire SAIPEN documents.

---

## Tests

Fixtures:

- project without `.saipen`;
- `.saipen` with valid STATE;
- `.saipen` with malformed STATE;
- Git clean;
- Git dirty;
- no Git;
- filesystem snapshot change;
- package contains manifest;
- package failure does not mutate `.saipen`.

---

## Acceptance gate

Wave D passes only if SAIPEN projects gain useful context, normal projects remain unaffected, full packaging still works, and no SAIPEN protocol file is modified.
