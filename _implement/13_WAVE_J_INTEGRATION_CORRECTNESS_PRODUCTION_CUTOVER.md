# AUDAPACK — WAVE J

## INTEGRATION CORRECTNESS / PRODUCTION CUTOVER / CURRENT BASELINE HARDENING

### BASELINE

This wave targets the current project represented by:

```text
_AUDAPACK_26-08-2026-T04-22-28.zip
```

Do not treat this as a greenfield implementation wave.

Substantial earlier work is already present and must be preserved.

The purpose of Wave J is to close verified integration defects between the already-built subsystems and turn the current implementation into a trustworthy v1 release candidate.

---

# 0. READ THIS FIRST — DO NOT REBUILD WORK THAT IS ALREADY CORRECT

The current implementation already contains important successful migrations:

- `AUDAPACK_WIDGET.user.js` is now the full mature AICHATBUTTONS-derived implementation rather than the earlier minimal stub;
- mature Auto3 / lease / ownership / recovery / lineage / attachment detection / audit caching / retry behavior is present;
- AUDAPACK Bridge exposes explicit service identity;
- Bridge `/health` reports API v2;
- `/v1/registry` and `/v1/projects/resolve` exist server-side;
- unknown projects can be auto-registered into `SIDE1+`;
- dynamic `SIDE2`, `SIDE3`, ... support exists;
- `%LOCALAPPDATA%\AUDAPACK` runtime/config/state/secrets layout has been introduced;
- Windows Scheduled Task support exists;
- legacy ACBBridge detection/takeover code exists;
- one history directory per run has been introduced;
- GUI polls a cross-process audit-generation signal.

These are foundations to preserve.

Do NOT:

- rewrite the 14k-line Widget from scratch;
- simplify Auto3;
- replace Tkinter;
- introduce Electron, SQLite, Node runtime, Docker, browser automation frameworks, or a web dashboard;
- restart old Waves A-I;
- redesign unrelated UI;
- perform mass cosmetic renames through mature AICHATBUTTONS internals.

This wave is surgical integration hardening.

---

# 1. VERIFIED CURRENT TEST BASELINE

Before implementation, reproduce and record the current baseline truthfully.

From the current project root, the unscoped command:

```text
python -m pytest -q
```

currently fails during collection because pytest discovers the copied reference project:

```text
_AICHATBUTTONS/ACBBridge/tests/test_bridge.py
```

and collides with:

```text
tests/test_bridge.py
```

The current scoped command:

```text
python -m pytest -q tests
```

currently produced in the inspected environment:

```text
62 passed
7 failed
1 skipped
```

The seven failures are all from `tests/test_clipboard_files.py` because `audapack/ui/clipboard_files.py` binds:

```python
ctypes.windll
```

at module import time, which crashes on non-Windows Python before the tests can exercise their intended platform guards.

There are also current Python `SyntaxWarning` messages caused by Windows paths inside ordinary docstrings such as `%LOCALAPPDATA%\AUDAPACK`.

Do not report an older `42 passed / 1 skipped` baseline as the current project result.

---

# 2. EXECUTION ORDER

Repair in this order:

1. WJ-001 — audit path containment;
2. WJ-002 — secret isolation and token lifecycle;
3. WJ-003 — transactional legacy bridge takeover/autostart;
4. WJ-004 — atomic cross-process registry mutation;
5. WJ-005 — strict canonical wave validation;
6. WJ-006 — canonical project/run identity and collision-safe state;
7. WJ-007 — atomic cross-process generation notification;
8. WJ-008 — Widget API v2 + registry handshake;
9. WJ-009 — real Tampermonkey migration;
10. WJ-010 — test/platform hygiene;
11. WJ-011 — single production surface and branding cleanup;
12. WJ-012 — Windows end-to-end production acceptance.

P0 correctness/security gates must be green before cosmetic cleanup.

---

# 3. WJ-001 — P0 AUDIT PATH CONTAINMENT

## Verified defect

`audapack/bridge/storage.py::resolve_project_audit_dir()` currently builds:

```python
target_dir = out_root / grp / proj.display_name
```

Although `sanitize_project_name()` exists in the same module, the resolved project directory does not use it.

A project can be auto-registered from incoming audit/browser identity, and `ProjectRegistry.resolve_or_register_project()` stores the raw display name.

A direct current-baseline probe demonstrated:

```text
../../escape
```

can resolve outside the configured audit root.

This is a real write-boundary defect.

## Required repair

Introduce one canonical filesystem-safe project directory name function and use it at the disk boundary.

Human identity and filesystem identity must be conceptually separate:

```text
project_id
human display_name
filesystem_name / safe audit directory name
```

It is acceptable to derive `filesystem_name` deterministically rather than persist another field if that keeps schema simpler.

Handle at least:

```text
< > : " / \ | ? *
control characters
trailing spaces/dots
.
..
CON PRN AUX NUL COM1..COM9 LPT1..LPT9
empty result
```

Do not silently allow absolute-drive, UNC, traversal, or rooted semantics through path joining.

## Mandatory containment check

After building the physical destination, resolve it and prove it is inside the configured audit root.

Semantically:

```python
resolved_target.relative_to(resolved_audit_root)
```

must succeed.

If containment fails:

- reject the request;
- do not auto-create the path;
- do not write any file;
- return a permanent logical error such as `invalid_project_path`.

Do not rely only on sanitization. Defense must exist at the final filesystem boundary.

## Regression matrix

Test at minimum:

```text
../../escape
..\..\escape
C:\Temp\Foo
\\server\share
.
..
CON
name.
name<bad>
foo/bar
foo\bar
Unicode valid project
normal ASCII project
```

Prove no case can write outside the configured audit root.

---

# 4. WJ-002 — P0 SECRET ISOLATION / PORTABLE CONFIG MUST BE SECRET-FREE

## Verified defect

The project has introduced `%LOCALAPPDATA%\AUDAPACK\secrets\token.txt`, which is the correct direction.

However the current config model still includes:

```python
BridgeConfig.token
```

and `AppConfig.to_dict()` currently serializes:

```python
"bridge": asdict(self.bridge)
```

Therefore the token still enters saved JSON config.

The current source archive also contains `audapack.json` with a populated real Bridge token.

This means excluding only `token.txt` from packing is insufficient.

## Required ownership model

Canonical token storage:

```text
%LOCALAPPDATA%\AUDAPACK\secrets\token.txt
```

Portable config may contain:

```text
host
port
autostart
max_request_bytes
history_retention_days
```

but must not contain the production token.

`BridgeConfig.token` may remain an in-memory runtime field if convenient, but ordinary serialization must explicitly omit it.

Do not use raw `asdict(self.bridge)` for persistent portable config if it contains secrets.

## Legacy migration

When loading old config containing `bridge.token`:

1. read it once;
2. migrate it to canonical secret storage;
3. preserve connectivity;
4. save sanitized config without the token;
5. do not repeatedly re-import stale project-local secret state on later launches.

After verified migration, source `audapack.json` must not remain a valid production secret copy.

Choose a safe migration strategy:

- redact/remove token field;
- migrate the whole legacy source config to user runtime and leave only a safe example/reference;
- or delete obsolete source config after successful runtime migration if project expectations allow.

Never destroy a valid config before the canonical runtime copy is verified.

## Remove hard-coded personal auth paths

`audapack/bridge/server.py::check_auth()` currently contains explicit paths such as:

```text
C:\Users\vac34\AppData\Local\ACBBridge\token.txt
C:\Users\vac34\AppData\Local\AUDAPACK\secrets\token.txt
C:\Users\vac34\AppData\Local\AUDAPACK\migration_backup\ACBBridge\token.txt
```

This must not remain production behavior.

Use canonical helpers based on `%LOCALAPPDATA%`.

Do not hard-code a username.

Legacy token acceptance must be migration-scoped, not an indefinite permanent authentication bypass.

After takeover/rotation succeeds, old ACBBridge backup token should no longer authenticate normal production requests unless an explicit bounded migration policy requires it.

## Token rotation

Because the current token has been present in source config and may already exist in generated project packages, rotate it after the migration path is proven.

Order matters:

```text
prepare new secret
→ make Widget able to receive/use it
→ verify authenticated new Bridge
→ retire old token
```

Do not rotate first and strand the installed Widget.

## Archive secret-content regression

A packing test that only checks filenames is insufficient.

Create a unique test token, package AUDAPACK, inspect every archive entry, and prove the token bytes occur zero times.

Expected:

```text
production/test secret token occurrences in resulting ZIP: 0
```

---

# 5. WJ-003 — P0/P1 TRANSACTIONAL LEGACY ACBRIDGE TAKEOVER

## Already implemented

Keep the existing:

- `detect_legacy_installation()`;
- `stop_verified_legacy_bridge()` ownership checks;
- `AUDAPACK Bridge` Scheduled Task manager;
- service identity verification.

Do not replace them wholesale.

## Verified remaining defect

`perform_bridge_takeover()` currently attempts `install_autostart()`.

If that operation fails, it appends an error but continues to Step 6 and may delete the legacy `ACBBridge` Scheduled Task anyway.

The function can later return success even though a mandatory takeover step failed.

That violates the migration safety contract.

## Hard gate

Legacy task removal is permitted only after all of these are proven:

```text
new AUDAPACK Bridge process started
service == AUDAPACK Bridge
api_version == supported version
authenticated status succeeds
registry endpoint succeeds
controlled write path succeeds or an equivalent write capability probe succeeds
AUDAPACK Bridge Scheduled Task installation succeeds
Scheduled Task is read back and command matches current AUDAPACK runtime
Scheduled Task can be manually triggered and produces healthy AUDAPACK Bridge
```

Only then delete:

```text
Scheduled Task: ACBBridge
```

## Failure semantics

Any mandatory failure:

- returns takeover failure;
- leaves legacy task intact;
- must not report COMPLETE;
- should restore/restart verified legacy Bridge when safely possible after it had already been stopped.

Do not kill arbitrary Python based only on port ownership.

## Backup timing

If legacy runtime is backed up, create or verify the backup before destructive cleanup where practical.

Backup failure need not always block takeover if the legacy source remains untouched, but report it truthfully.

---

# 6. WJ-004 — P1 CROSS-PROCESS ATOMIC PROJECT REGISTRY

## Verified remaining defect

`ProjectRegistry.resolve_or_register_project()` currently:

```text
reads its in-memory config
checks existing
finds free SIDE slot
appends project
save_config()
returns success
```

There is no cross-process transaction.

Bridge is threaded and GUI is a separate process/writer.

Two stale registry instances can allocate the same slot or last-write-wins away another project.

Also, `save_config()` returns `False` on failure, but `resolve_or_register_project()` currently ignores that return and can return a newly-created project as though registration succeeded.

## Canonical transaction

For every mutating registry operation that can race across processes:

```text
acquire cross-process registry lock
→ reload latest config from canonical disk
→ validate latest config
→ resolve existing project again
→ allocate slot against latest state
→ mutate latest state
→ atomic save
→ verify/save result
→ release lock
```

Do not use only `threading.Lock`.

Use a lightweight Windows-compatible cross-process primitive. Avoid database introduction.

## Applies to

At minimum:

- auto-register unknown project;
- moves/swaps if Bridge and GUI can both affect placement;
- removal where concurrent writes can occur;
- any helper that saves a stale `AppConfig` over newer project state.

A centralized config/registry transaction helper is preferable to ad-hoc locks.

## Save failure

If atomic config persistence fails:

- do not return `registered` success;
- do not return a project assignment the next request cannot observe;
- report a retriable or permanent configuration error based on cause.

## Concurrency tests

Test concurrent same-name registration:

```text
N requests for BananaTool
→ exactly one project
→ exactly one project_id
→ exactly one slot
```

Test many distinct projects:

- no duplicate slot;
- no lost entries;
- no malformed JSON;
- correct `SIDE1 -> SIDE2 -> SIDE3` growth.

---

# 7. WJ-005 — P1 STRICT CANONICAL AUDIT WAVE VALIDATION

## Verified defect

Current `parse_wave()` mainly requires the configured DONE marker plus `TICKETS` and ticket counts.

A direct current-baseline probe showed that this invalid Core still returns valid:

```text
PROJECT_NAME: X
WAVE: AUDIT CORE
TICKETS: 0
CORE_DONE_WHEN: done
```

It lacks:

```text
STATUS: AUDIT_CORE: COMPLETE
```

A Performance text containing `CORE_DONE_WHEN:` can also be accepted when parsed as Core.

Therefore canonical disk materialization can accept wrong/incomplete wave content.

## Required exact contracts

### Core

Require exact meaningful fields:

```text
PROJECT_NAME:
WAVE: AUDIT CORE
STATUS: AUDIT_CORE: COMPLETE
TICKETS: <integer >= 0>
CORE_DONE_WHEN:
```

### Second

Require:

```text
PROJECT_NAME:
WAVE: AUDIT SECOND WAVE
STATUS: SECOND_WAVE: COMPLETE
TICKETS:
SECOND_WAVE_DONE_WHEN:
```

### Performance

Require:

```text
PROJECT_NAME:
WAVE: AUDIT PERFORMANCE / STABILITY / EFFECTIVENESS
STATUS: PERFORMANCE: COMPLETE
TICKETS:
PERFORMANCE_DONE_WHEN:
```

Match header values, not mere substring occurrence.

## Ticket consistency

When `TICKETS > 0`, validate unique expected ticket IDs:

```text
CORE-###
W2-###
PERF-###
```

Do not allow a Core parser to count W2/PERF tickets as valid Core evidence.

When `TICKETS: 0`, accept the documented zero-ticket terminal form.

## One server-side authority

Bridge physical persistence and `AuditIndexer` readiness should converge on one canonical validation contract where practical.

Browser execution detection may remain separate because it solves a different live-DOM problem, but disk authority must be strict.

## Required false-positive regressions

Reject:

```text
Core without STATUS
Performance text passed as Core
Second text passed as Performance
wrong WAVE header
wrong terminal STATUS
wrong DONE marker
negative TICKETS
TICKETS count inconsistent with unique ticket IDs
```

Accept structurally correct real examples from all three waves.

---

# 8. WJ-006 — P1 CANONICAL PROJECT/RUN OWNERSHIP + COLLISION-SAFE RUN STATE

## Current defect A — run bound to textual project

Bridge run state currently stores:

```text
state["project"]
```

and checks mismatch using lowercased display strings.

This is weaker than the existing canonical registry identity.

## Required invariant

After the first valid accepted wave:

```text
run_id -> project_id
```

is immutable.

Store at least:

```text
project_id
project_display_name (diagnostic only)
```

Resolve identities before comparing them.

Formatting variants that resolve to the same project must not produce a false mismatch.

## Payload vs handoff conflict

If request contains:

```text
project_id = fastprompter
```

but validated audit content says:

```text
PROJECT_NAME: SAIPEN
```

resolve both.

If they map to different canonical IDs:

```text
409 project_identity_conflict
```

No physical write.

Do not route by one identity and store state under another.

## Current defect B — run-state filename collision

`audapack/bridge/state.py::sanitize_run_id()` currently sanitizes raw run ID and truncates to 64 characters.

Distinct full run IDs can therefore map to the same state filename.

Meanwhile locks are keyed by original raw run ID, so two different locks can write the same physical state file.

## Required repair

Derive persistent state file identity from cryptographic hash of full run ID.

Example:

```text
run_<sha256-prefix-at-least-16-hex>.json
```

Store full original `run_id` inside JSON.

The lock key and state-file key must refer to the same canonical full-run identity.

## Migration

If legacy sanitized state files may already exist, migrate/read them safely without overwriting unrelated hashed state.

## Regression

Two run IDs deliberately designed to sanitize/truncate identically must remain independent.

---

# 9. WJ-007 — P1 CROSS-PROCESS AUDIT GENERATION SIGNAL

## Verified remaining defect

`increment_audit_generation()` is documented as atomic but currently performs:

```text
read JSON
increment in memory
write temp
replace
```

without a cross-process lock around read-modify-write.

Two simultaneous writes can both read generation N and both publish N+1.

Atomic replace prevents partial file corruption; it does not make the increment atomic.

## Required repair

Use either:

### Option A

Cross-process lock around:

```text
read -> increment -> atomic write
```

### Option B

Replace the counter with unique event identity where lost increments are impossible and GUI only needs change detection.

Keep solution lightweight.

## Acceptance

Concurrent test must prove all emitted updates remain detectable and monotonic according to the chosen contract.

---

# 10. WJ-008 — P1 COMPLETE WIDGET ↔ AUDAPACK REGISTRY / API V2 HANDSHAKE

## Already correct

The Widget is now the mature full AICHATBUTTONS-derived codebase. Preserve this.

The Bridge already provides:

```text
GET /health
GET /v1/registry
POST /v1/projects/resolve
POST /v1/audits
```

and server `/health` reports:

```text
service = AUDAPACK Bridge
api_version = 2
```

## Verified remaining defect

The current Widget still declares:

```javascript
const BRIDGE_API_VERSION = 1;
```

It also currently only inserts:

```javascript
project_id: job.projectId || ''
```

into delivery payload, but the current code does not actually resolve and populate `job.projectId` through the new registry API.

The Widget still contains many user-facing `ACBBridge` labels.

## Required behavior

When project identity becomes stable enough for audit automation:

```text
project_name
→ POST /v1/projects/resolve
→ project_id + group + slot + registry revision
```

Store logical route in active audit runtime.

Suggested fields:

```text
projectName
projectId
projectGroup
projectSlot
registryRevision
projectResolutionStatus
```

Do not store physical Windows paths in browser state.

## Route display

Add a small status surface only; do not redesign Widget.

Examples:

```text
PROJECT: FastPrompter · MAIN0/1
PROJECT: NewTool · SIDE1/4 · NEW
PROJECT: SAIPEN · WAITING FOR BRIDGE
```

## API version

Move new Widget contract to API v2.

Bridge must also validate incoming `api_version` rather than ignoring it.

Unsupported version should receive explicit permanent error:

```text
unsupported_api_version
```

## Health identity

Widget health check must distinguish:

```text
CONNECTED
OFFLINE
AUTH ERROR
WRONG SERVICE
API INCOMPATIBLE
```

Do not treat arbitrary HTTP success or legacy ACBBridge as AUDAPACK Bridge.

## Keep compatibility deliberately

If retaining `X-ACB-Token` or internal `acb-*` names avoids risky broad migration, that is acceptable internally.

User-facing product wording should say:

```text
AUDAPACK Widget
AUDAPACK Bridge
```

Do not perform a dangerous 14k-line mass rename merely for cosmetic purity.

---

# 11. WJ-009 — P1 REAL TAMPERMONKEY MIGRATION

## Requirement

Current installed AICHATBUTTONS behavior/settings matter.

Do not assume a differently identified userscript can read another userscript's GM storage merely because it knows the old key name.

Verify real Tampermonkey behavior.

## Preserve durable user state where safe

At minimum verify preservation/migration of relevant:

- categories;
- presets/custom commands;
- panel size/position;
- Auto3 preference;
- prompt-delivery preference;
- bridge URL/config;
- bridge authentication token;
- other durable preferences.

Ephemeral per-conversation runtime may be intentionally reset if carrying it across script identity is unsafe; document that distinction.

## Allowed migration strategies

Prefer the smallest actually verified path:

1. temporarily retain compatible userscript identity for an upgrade release;
2. explicit one-time export/import;
3. bridge-assisted migration.

Do not claim migration success from an unverified `GM_getValue(oldKey)` call under a new storage sandbox.

## Idempotency

Running migration twice must not duplicate presets/categories or overwrite newer AUDAPACK settings with older legacy state.

---

# 12. WJ-010 — P1/P2 TEST DISCOVERY + PLATFORM BOUNDARIES

## Root pytest contamination

Keep `_AICHATBUTTONS` as intentional read-only reference material, but exclude it from AUDAPACK test discovery.

Add `pytest.ini` or equivalent.

Expected root command:

```text
python -m pytest -q
```

must execute only intended AUDAPACK tests and must not collide with reference `test_bridge.py`.

Use `testpaths = tests` and/or appropriate `norecursedirs`.

Do not delete reference project just to make pytest green.

## Clipboard non-Windows import defect

`audapack/ui/clipboard_files.py` must not bind Win32 DLL symbols at module import time on non-Windows.

Move Win32 binding behind:

```text
sys.platform == "win32"
```

or lazy runtime setup.

Requirements:

- module imports on non-Windows;
- platform-neutral payload builder tests run;
- public copy helper returns defined unsupported/failure result on non-Windows;
- Win32-specific behavior remains unchanged on Windows.

Do not simply skip all clipboard tests on non-Windows if pure helper logic can be tested there.

## Docstring warnings

Fix invalid escape-sequence warnings in Windows-path docstrings by using raw strings or escaped backslashes where appropriate.

No behavioral change required.

## Current scoped-suite gate

The current seven clipboard failures must be accounted for with actual repair, not hidden by blanket skip.

---

# 13. WJ-011 — P2 SINGLE PRODUCTION SURFACE + LEGACY USER-FACING DRIFT

## Legacy launcher

Current `START_GUI.bat` still launches:

```text
pack_all_audit_gui.py
```

while `START_AUDAPACK.cmd` launches the new application.

There must not be two production GUIs.

Convert `START_GUI.bat` into a compatibility redirect to the canonical new AUDAPACK entry point, or remove it from production surface after verifying no required workflow depends on it.

Do not leave a launcher that silently starts the old independent application.

## Legacy source

If `pack_all_audit_gui.py` remains for reference/migration, clearly mark/isolate it as legacy reference and ensure normal launch/context-menu/autostart never uses it.

## Widget user-facing wording

Update visible strings such as:

```text
Use ACBBridge
ACBBridge connected
ACBBridge token
Audit Disk Bridge
```

into AUDAPACK product wording.

Internal DOM IDs / legacy storage keys may remain for backward compatibility where safe.

## Reference packaging

The copied `_AICHATBUTTONS` tree is intentional reference material.

It should not be required at runtime and should not accidentally inflate ordinary AUDAPACK release/package output unless explicitly requested.

Prove production starts/functions when reference tree is temporarily unavailable.

---

# 14. WJ-012 — REAL WINDOWS END-TO-END PRODUCTION ACCEPTANCE

Unit tests are not enough for this wave.

Run real Windows smoke checks when the execution environment permits.

Do not fabricate platform verification when running in Linux/sandbox.

## Scenario A — Bridge task ownership

Verify actual Windows task:

```text
AUDAPACK Bridge
```

Inspect actual command.

It must point to current AUDAPACK production path, not `_AICHATBUTTONS\ACBBridge`.

Stop Bridge, manually trigger the Scheduled Task, and verify:

```text
service = AUDAPACK Bridge
api = 2
auth works
registry works
```

## Scenario B — existing project

Use a real existing registry project, e.g. FastPrompter.

Expected:

```text
Widget detects project
→ resolve returns stable project_id/current group/current slot
→ audit delivery accepted
→ Bridge re-resolves current registry
→ canonical file appears in correct MAIN/SIDE project folder
```

## Scenario C — new project

Use unique temporary name:

```text
AUDAPACK_ROUTING_TEST_<timestamp>
```

Expected:

```text
unknown
→ atomic auto-register
→ first free SIDE1+ slot
→ GUI observes project
→ audit written there
```

## Scenario D — move after registration

Move temporary project in GUI to another group/slot.

Next audit must follow current registry location without Widget path reconfiguration.

## Scenario E — offline queue + move

Stop Bridge.

Queue completed audit in Widget.

Move project while Bridge offline.

Restart Bridge.

Queued job must materialize according to current project placement, not stale cached group.

## Scenario F — full Auto3

Run:

```text
CORE
→ SECOND
→ PERFORMANCE
```

Require:

- three strictly validated wave files;
- same canonical `project_id`;
- one run history identity;
- canonical ALL_3 only after 3/3;
- correct current project directory.

## Scenario G — GUI NEW/COPIED

After new ALL_3:

```text
GUI → NEW
COPY AUDIT → exact ALL_3 clipboard → ✓ COPIED
```

Then generate a later ALL_3 and verify copied state resets to NEW.

## Scenario H — secret scan

Pack AUDAPACK.

Scan every archive file content for current production token.

Expected occurrences:

```text
0
```

## Scenario I — legacy absence

After successful takeover, temporarily make old AICHATBUTTONS/ACBBridge runtime unavailable without destroying reference evidence.

Verify:

- Bridge starts;
- task works;
- Widget connects;
- registry resolves;
- audit persists;
- ALL_3 generates;
- GUI refreshes.

This proves production independence.

---

# 15. REGISTRY REVISION CONTRACT — FIX CURRENT DRIFT

Current Bridge responses use inconsistent meanings for `registry_revision` / `revision`:

- health may use project count;
- registry endpoint may use current wall-clock seconds;
- resolve response may use project count.

This is not a stable cache/change contract.

Choose one canonical revision identity.

Lightweight acceptable options:

- hash of canonical project-registry JSON;
- config file mtime_ns plus validation;
- explicit monotonically increasing persisted revision under the same registry transaction.

All endpoints must report the same revision for the same registry state.

Widget caches against that revision only if useful.

Do not invent different revision semantics per endpoint.

---

# 16. LIVE CONFIG RELOAD / LAST-KNOWN-GOOD

Bridge already reloads config in request paths. Preserve that behavior, but ensure registry transactions and live reads share the same canonical source.

If a config write is temporarily malformed/partial despite atomic protections:

- never route using empty fallback state;
- retain last-known-good in memory when safe;
- report configuration error rather than silently writing elsewhere.

Never fall back to current working directory for audit output.

---

# 17. ALL_3 CORRECTNESS GATE

Canonical ALL_3 is valid only when:

```text
Core = strict valid
Second = strict valid
Performance = strict valid
same run_id
same canonical project_id
```

Do not build ALL_3 merely because three state keys exist.

If any wave later becomes invalid/conflicting, do not preserve a misleading new ALL_3 success state.

Canonical disk ALL_3 remains Bridge-owned.

---

# 18. HISTORY BEHAVIOR WHEN PROJECT MOVES MID-RUN

Current code stores absolute `history_dir` in run state.

Decide and document one invariant for project movement during an active/queued run.

Preferred behavior:

- canonical latest audit files always follow current registry at physical delivery time;
- one run history remains one coherent directory and never splits into multiple history directories;
- history location must not cause writes outside canonical audit root after project move/config change.

If history remains anchored to its first valid wave location, make that deliberate and safe.

If history should move with project, implement one atomic migration policy.

Do not accidentally mix current latest files in one group with uncontrolled absolute history path elsewhere.

---

# 19. ERROR CLASSIFICATION FOR WIDGET QUEUE

Preserve mature durable retry behavior.

Classify errors so retry does not become an infinite poison loop.

Retryable examples:

```text
bridge offline
output root temporarily unavailable
filesystem temporarily busy
transient internal write error
```

Permanent/request-repair examples:

```text
unsupported_api_version
invalid_wave_structure
project_identity_conflict
receipt_conflict
invalid_project_path
wrong service
```

Do not delete the locally cached completed audit evidence merely because delivery is permanently rejected.

Surface diagnosis in Widget state.

---

# 20. SECURITY / PORTABILITY CLEANUP

Audit current production code for any remaining literal machine/user paths introduced only for temporary migration/debug purposes.

User-specific absolute paths belong in:

- defaults where intentionally product-specific;
- config;
- migration evidence docs;

not in generic runtime auth/security logic.

Do not remove the intended default audit root/project templates merely because they are user-specific configuration defaults; distinguish those from hidden hard-coded security fallbacks.

---

# 21. TEST MATRIX — REQUIRED ADDITIONS

## Security/path

- traversal forward slash;
- traversal backslash;
- absolute drive;
- UNC;
- Windows reserved names;
- trailing dot/space;
- valid Unicode;
- destination containment;
- secret absent from serialized canonical config;
- secret absent from package contents;
- hard-coded username auth fallback absent;
- wrong service rejected;
- non-loopback binding remains rejected.

## Registry

- concurrent same project;
- concurrent different projects;
- save failure does not return false success;
- SIDE1 full -> SIDE2;
- SIDE2 full -> SIDE3;
- no duplicate slot;
- current registry after move;
- canonical registry revision stable/changes correctly.

## Audits

- missing exact STATUS rejected;
- wrong WAVE rejected;
- wrong DONE marker rejected;
- performance-as-core rejected;
- ticket mismatch rejected;
- project payload/handoff conflict rejected;
- name aliases resolving to same ID accepted;
- run/project ID lock;
- receipt duplicate same content;
- receipt conflict different content;
- all3 only after 3 strict waves.

## State/concurrency

- colliding sanitized run IDs remain separate;
- concurrent generation updates obey contract;
- one coherent history directory per run;
- project move does not escape history/output root.

## Migration

- new autostart install failure preserves legacy task;
- wrong task command blocks legacy deletion;
- failed identity/auth/registry/write probe blocks cutover;
- success removes legacy task only after proof;
- stale AUDAPACK task path repair;
- legacy source absence after successful takeover.

## Platform/tests

- `python -m pytest -q` clean collection;
- clipboard module imports on non-Windows;
- pure clipboard payload helpers test cross-platform;
- Windows copy behavior remains Windows-tested/mocked as appropriate;
- no invalid escape warnings from touched path docstrings.

---

# 22. JS REGRESSION DISCIPLINE

The full Widget is mature inherited code.

Run:

```text
node --check resources/AUDAPACK_WIDGET.user.js
```

Run the inherited JS regression harness where available.

Do not weaken tests only to make migration green.

If inherited baseline tests are already red in the reference AICHATBUTTONS under the same harness, classify them separately from new AUDAPACK regressions.

Migration invariant:

```text
AUDAPACK Widget must not introduce new failures relative to the same AICHATBUTTONS baseline.
```

---

# 23. SAIPEN PROJECT STATE

This project now contains root:

```text
.saipen/STATE.md
.saipen/BOARD.md
.saipen/LOG.md
```

Treat root `.saipen` as project continuation state.

Rules:

- live implementation remains authoritative for runtime behavior;
- update BOARD tickets as repairs land;
- append meaningful LOG events;
- update STATE `next_action` last at checkpoint;
- do not mutate the copied `_AICHATBUTTONS/.saipen` reference as though it were AUDAPACK state;
- root `.saipen` belongs to AUDAPACK.

Checkpoint ordering:

```text
LOG -> BOARD -> STATE last
```

Do not use `.saipen` as an excuse to broaden scope into SAIPEN protocol development.

---

# 24. DO NOT CLOSE WAVE ON SANDBOX-ONLY CLAIMS

Some Windows/Tampermonkey behavior cannot be truthfully verified in a Linux execution sandbox.

In that case:

- implement static/unit coverage;
- report environment limitation;
- leave Windows/Tampermonkey acceptance explicitly pending;
- do not fabricate Scheduled Task or browser results.

Wave may be implementation-complete with a clearly identified external smoke gate, but must not claim `PRODUCTION_CUTOVER_VERIFIED` until real Windows evidence exists.

---

# 25. CHECKPOINT GATES

## Gate A — filesystem/security

Must be green before proceeding:

```text
path containment
portable config secret-free
archive secret-content scan
canonical auth paths only
wrong service detection
```

## Gate B — state/registry correctness

Must be green:

```text
cross-process registry transaction
no false-success save
strict wave validator
project_id run ownership
hashed run-state identity
atomic generation contract
```

## Gate C — Widget integration

Must be green:

```text
API v2
service identity
project resolve
project_id populated
route status
current-placement queued delivery
```

## Gate D — migration

Before legacy task removal:

```text
new Bridge healthy
new Bridge authenticated
registry works
controlled write works
new Scheduled Task installed
new Scheduled Task command verified
new Scheduled Task start verified
```

## Gate E — release candidate

```text
root pytest intended suite green or only explicitly justified platform skips
JS syntax green
no new JS regression versus reference
Windows smoke completed or truthfully marked pending
secret scan zero occurrences
old runtime no longer required after verified cutover
```

---

# 26. DEFINITION OF DONE

Wave J is complete only when the implementation satisfies these invariants:

### Filesystem

No browser/audit project identity can cause canonical writes outside configured audit root.

### Secrets

Production Bridge secret exists only in intended private runtime storage and is absent from portable config and generated AUDAPACK package content.

### Registry

One logical project produces one atomic registry entry/slot even under concurrent writers.

### Run identity

One run is bound to one immutable canonical `project_id`.

### Validation

Only exact structurally complete Core/Second/Performance waves enter canonical storage.

### Bridge identity

HTTP success alone never proves AUDAPACK ownership.

### Widget

Full mature AICHATBUTTONS behavior remains, while routing uses AUDAPACK API v2 and canonical project identity.

### Routing

Existing project follows current registry; new project auto-registers into first free `SIDE1+`; queued delivery follows current placement.

### Migration

Legacy `ACBBridge` task is removed only after new `AUDAPACK Bridge` startup/autostart is proven.

### Product surface

Normal launchers/context menu/autostart point to one AUDAPACK product, not an independent old GUI/bridge.

### Tests

Reference trees do not contaminate root test discovery.

---

# 27. FINAL HANDOFF FORMAT

When this wave is exhausted, return one standalone handoff:

```text
AUDAPACK WAVE J: COMPLETE | PARTIAL | BLOCKED

BASELINE:
_AUDAPACK_26-08-2026-T04-22-28.zip + actual current revision/hash

SAIPEN:
- root state:
- BOARD status:
- last LOG event:

SECURITY:
- path containment:
- token storage:
- portable config token:
- package secret scan:
- auth path cleanup:

REGISTRY:
- transaction mechanism:
- concurrency result:
- SIDE1+ allocation:
- registry revision:

AUDIT VALIDATION:
- Core:
- Second:
- Performance:
- ALL_3 gate:

RUN STATE:
- project_id ownership:
- state filename identity:
- history policy:
- generation signal:

WIDGET:
- version:
- API version:
- service identity check:
- project resolve:
- route display:
- AICHATBUTTONS parity:
- Tampermonkey migration:

BRIDGE:
- version:
- service identity:
- auth:
- current-placement routing:
- error classification:

AUTOSTART / LEGACY TAKEOVER:
- AUDAPACK task actual command:
- task trigger verification:
- legacy ACBBridge state:
- rollback behavior:

TESTS:
- python -m pytest -q:
- scoped suite if separately run:
- node --check:
- inherited JS harness:

WINDOWS END-TO-END:
- existing project:
- new project:
- move:
- offline queue:
- full Auto3:
- GUI NEW/COPIED:
- legacy absence:
- secret ZIP scan:

KNOWN LIMITATIONS:
<NONE or truthful exact list>

NEXT_ACTION:
If all production gates are verified -> execute FINAL ACCEPTANCE AUDIT.
Otherwise -> continue this same Wave J only on remaining red gates.
```

Do not create a new feature wave while any Wave J P0/P1 gate remains unresolved.
