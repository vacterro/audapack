# WAVE F — AICHATBUTTONS → AUDAPACK WIDGET MIGRATION

## Goal

Migrate the useful browser-side capabilities of AICHATBUTTONS into a bundled AUDAPACK component without preserving known architectural defects or external runtime dependency on the old project.

This is a migration, not a copy-paste ceremony.

---

## Before coding

Read:

1. actual AICHATBUTTONS userscript;
2. ACBBridge scripts/config;
3. userscript tests;
4. latest `AI ChatButtons__00_AUDIT_ALL_3.md`.

Build a capability map:

```text
feature
current owner
current persistence
known defects
new AUDAPACK owner
migration decision
```

---

## Required work

### F1. Bundled resource

Create a pinned resource such as:

```text
resources\AUDAPACK_WIDGET.user.js
```

The new AUDAPACK runtime must not require the old `_AICHATBUTTONS` folder.

If an `_AICHATBUTTONS` folder exists inside the AUDAPACK working tree, treat it as a **read-only reference snapshot intentionally supplied by the user**. Do not delete it merely because it exists. Do not import it at runtime, execute its legacy bridge, register its scripts, or package it into a normal AUDAPACK release.

### F2. Preserve useful generic behavior

Retain working capabilities that still make sense:

- supported AI chat sites;
- prompt buttons/categories;
- custom presets;
- audit prompt presets;
- Auto3;
- project-name extraction;
- attachment awareness;
- stage state;
- lineage;
- continuation/recovery;
- conversation ownership;
- lease/fencing;
- SPA root replacement resilience;
- durable queue;
- bounded retry/backoff.

Do not collapse mature ownership logic into naive polling.

### F3. Separate generic chat from audit automation

AUDAPACK-specific audit logic must have a clear mode/boundary.

A normal chat must not accidentally activate audit sequencing.

### F4. New namespace

Use an AUDAPACK namespace for new userscript persistence, e.g.:

```text
audapack_widget_*
```

### F5. Legacy migration

On first compatible run:

1. detect recognized `ai_chatbuttons_*` keys;
2. import known settings;
3. normalize schema;
4. save new state;
5. keep old state intact initially;
6. mark migration version.

Migration must be idempotent.

### F6. Browser delivery queue

Preserve durable queue behavior so a completed audit survives:

- page reload;
- SPA route;
- temporary bridge outage;
- AUDAPACK restart.

Do not consider delivery complete until bridge confirms durable acceptance.

### F7. Performance cleanup

Do not migrate known hot-path waste.

Status rendering must not:

- rescan entire transcript repeatedly;
- enumerate all GM keys multiple times per render;
- write huge state blobs for trivial label changes.

Compute bounded snapshots and reuse them.

### F8. Component metadata

Widget reports:

- widget version;
- schema version;
- bridge API version expected;
- last-seen health where appropriate.

AUDAPACK GUI will use this in the next wave.


### F9. Registry-aware audit behavior

The fresh Tampermonkey script still performs the existing audit workflow (Core -> Second Wave -> Performance / recovery as applicable), but project placement is now AUDAPACK-aware.

The Widget obtains a **sanitized logical registry snapshot** from AUDAPACK Bridge. At minimum each visible routable project record provides:

```text
project_id
display_name
priority_group  # MAIN0 / MAIN1 / SIDE0 / SIDE1
slot            # 1..6
```

The browser must not receive or persist arbitrary Windows destination paths.

Expected behavior:

1. detect/extract the audit project from the conversation/project attachment context;
2. match it deterministically to the AUDAPACK registry;
3. display the resolved placement where useful, e.g. `MAIN0 / 1`;
4. enqueue completed waves using stable `project_id`;
5. if project is unknown/ambiguous, keep the audit durable and mark it unresolved instead of guessing;
6. Bridge performs final routing authorization.

Registry refresh must be bounded and cached. Do not fetch it on every DOM mutation or status repaint. Refresh on bridge reconnect, explicit settings refresh, detected registry revision change, or a sensible TTL.

### F10. Bridge API compatibility contract

Define the Widget-side contract before implementing the new bridge takeover. Expected logical endpoints/capabilities may include:

```text
GET /health
GET /v1/projects
POST /v1/audits/wave
```

Exact endpoint spelling may differ, but version it and test it. `GET /v1/projects` exposes logical routing metadata only, never arbitrary local filesystem roots.

---

## Tests

Adapt useful AICHATBUTTONS tests:

- lease;
- ownership;
- lineage;
- SPA observer;
- reload recovery;
- continuation;
- snapshot;
- queue durability;
- audit completion detection;
- settings persistence;
- legacy namespace migration;
- registry snapshot fetch/cache;
- deterministic project matching;
- unknown/ambiguous project remains queued;
- no physical filesystem paths are required by the Widget.

Add counters/guards for:

- GM list calls;
- GM get/set volume;
- DOM scan count;
- transcript extraction count.

---

## Acceptance gate

Wave F passes only if the bundled AUDAPACK Widget preserves required behavior, survives reload/recovery cases, and no longer depends on the old AICHATBUTTONS source folder at runtime.

Do not build the final bridge integration before the Widget contract is stable.
