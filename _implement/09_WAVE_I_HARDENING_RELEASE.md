# WAVE I — HARDENING, PERFORMANCE, WINDOWS SMOKE, RELEASE

## Goal

Harden the now-integrated AUDAPACK system, prove the old AICHATBUTTONS bridge is no longer a production dependency, and prepare a truthful v1 release.

Do not add unrelated features in this wave.

---

## I1. Re-read relevant audits

Re-check latest relevant audit handoffs for AICHATBUTTONS, AUDAPACK and SAIAUDIT reference architecture.

Verify migrated code did not reintroduce:

- history filename collisions;
- fail-open output roots;
- same-run read/modify/write races;
- project identity mixing;
- receipt ordering bugs;
- duplicate ALL_3 implementations;
- weak run-id identity;
- unsafe PID killing;
- duplicate bridge owners;
- stale `ACBBridge` Scheduled Task;
- repeated queue enumeration;
- giant GM state writes;
- repeated full transcript scans;
- physical filesystem paths exposed to the browser unnecessarily.

---

## I2. Full automated regression

Run all AUDAPACK tests, including migrated Widget tests and bridge concurrency tests.

Provide one-command test runner if absent.

Record exact commands and exact pass/fail counts.

---

## I3. Windows migration smoke

On target Windows, verify the real legacy takeover where possible:

```text
old task: ACBBridge
new task: AUDAPACK Bridge
old source: V:\___VAC\__K\__CODE\_TAMPERMONKEY\_AICHATBUTTONS\ACBBridge
new source: V:\___VAC\__K\__CODE\_PY\_AUDAPACK
```

Verify:

- new task exists;
- old task is absent/disabled after successful takeover;
- new task action points to AUDAPACK path;
- `%LOCALAPPDATA%\ACBBridge\app` is not the active runtime;
- new bridge starts at logon/task start;
- health identifies AUDAPACK Bridge;
- port ownership is correct;
- token is preserved or intentionally migrated;
- Repair is idempotent.

Do not claim Windows smoke PASS if not actually executed.

---

## I4. End-to-end audit routing smoke

Perform at least one real project in each available/meaningful group, ideally covering multiple groups:

```text
browser audit
-> AUDAPACK Widget
-> AUDAPACK Bridge
-> registry
-> MAIN/SIDE project folder
-> canonical wave files
-> ALL_3
-> GUI NEW
-> COPY AUDIT
-> ✓ COPIED
```

Then produce a second fresh ALL for the same project and verify `COPIED` resets to `NEW`.

At least one test must prove a project placed in e.g. `MAIN0` cannot accidentally land in the old flat project-subdir layout.

---

## I5. Registry movement smoke

Move a test project from one priority group to another through AUDAPACK.

Verify:

- registry revision changes;
- Widget eventually sees updated logical placement;
- next audit run goes to the new group;
- stale Widget cache cannot force the old group;
- existing historical material is not silently destroyed.

---

## I6. Reference snapshot independence

The copied `_AICHATBUTTONS` directory inside AUDAPACK is allowed to remain as development reference material.

Prove production independence by making that reference unavailable temporarily (rename or otherwise exclude it from runtime lookup).

AUDAPACK must still:

- launch;
- package projects;
- manage context menu;
- serve AUDAPACK Bridge;
- expose/install the bundled AUDAPACK Widget;
- fetch logical project registry;
- route audits;
- index audits;
- copy ALL_3.

Restore the reference afterward if desired.

---

## I7. Packaging exclusion

Normal AUDAPACK release/package output must not accidentally include the entire reference `_AICHATBUTTONS` repo, especially its `.git`, old `ACBBridge` runtime and historical development debris.

The reference may remain in the working tree, but mark it excluded from normal release packaging.

---

## I8. Full GUI smoke

Verify:

- launch;
- priority room;
- six slots each group;
- empty slots;
- pack one;
- pack selected;
- pack enabled;
- cancel;
- context-menu install/remove;
- silent Explorer pack;
- audit temperature;
- COPY AUDIT;
- Widget install/update;
- Bridge start/stop;
- Bridge repair;
- autostart;
- unresolved audit inbox;
- registry reassignment;
- no focus stealing / runaway UI refresh.

---

## I9. Performance

Measure/guard:

- browser DOM scans;
- transcript extraction count;
- GM list/get/set calls;
- registry HTTP refresh frequency;
- bridge queue enumeration;
- audit directory scans;
- GUI responsiveness during packing.

Registry-aware routing must not become a per-mutation network call.

---

## I10. Logging and diagnostics

Use bounded/rotated logs with categories such as:

```text
PACK
AUDIT
BRIDGE
COMPONENT
MIGRATION
```

Never log bridge token or full audit body by default.

User-facing failures explain:

```text
WHAT FAILED
WHY
WHAT TO DO
```

---

## I11. Documentation

README must match the real final workflow:

- What is AUDAPACK;
- Quick Start;
- project priority room;
- packaging;
- Explorer context menu;
- audit temperature/readiness;
- COPY AUDIT;
- SAIPEN bonus;
- AUDAPACK Widget;
- registry-aware audit targeting;
- AUDAPACK Bridge;
- migration from old ACBBridge;
- autostart/Repair;
- `_UNASSIGNED`;
- config/state locations;
- CLI;
- troubleshooting.

Include the canonical working source path used by this installation if appropriate, but do not hardcode it in generic portable logic where config/root discovery is better.

---

## I12. Release identity

All user-facing production components must say AUDAPACK.

Legacy names are allowed only in:

- migration detector;
- compatibility code;
- migration docs/log labels;
- read-only reference tree.

Set coherent compatible versions for desktop, Widget, Bridge API and config schema.

---

## Acceptance gate

Wave I passes only if automated tests are green, the integrated pipeline is proven, migration/autostart is correct, the old bridge cannot compete, normal releases exclude the reference repo, and AUDAPACK remains lightweight.

Then run the independent final acceptance audit.
