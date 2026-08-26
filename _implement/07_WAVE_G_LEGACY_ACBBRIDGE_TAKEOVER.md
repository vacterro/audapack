# WAVE G — LEGACY ACBBridge TAKEOVER AND AUTOSTART REPAIR

## Goal

Perform a controlled one-time migration from the currently installed legacy AICHATBUTTONS bridge to **AUDAPACK Bridge**, with the production code owned by:

```text
V:\___VAC\__K\__CODE\_PY\_AUDAPACK
```

This wave is specifically about installation ownership, old-process detection, Scheduled Task repair, state/token preservation, and rollback safety.

Do not implement final MAIN/SIDE routing here beyond the minimum needed to prove the new bridge can start. That belongs to Wave H.

---

## Known legacy installation

The existing bridge was installed from:

```text
V:\___VAC\__K\__CODE\_TAMPERMONKEY\_AICHATBUTTONS\ACBBridge\INSTALL.cmd
```

The inspected legacy installer creates:

```text
Scheduled Task: ACBBridge
Local root:      %LOCALAPPDATA%\ACBBridge
Copied runtime:  %LOCALAPPDATA%\ACBBridge\app
Config:          %LOCALAPPDATA%\ACBBridge\config.json
Token:           %LOCALAPPDATA%\ACBBridge\token.txt
PID file:        %LOCALAPPDATA%\ACBBridge\bridge.pid
Default bind:    127.0.0.1:17843
```

It starts copied code from:

```text
%LOCALAPPDATA%\ACBBridge\app\acbbridge.py
```

and registers the task named exactly:

```text
ACBBridge
```

Do not guess about this legacy state. Detect it explicitly.

---

## New canonical identity

Use:

```text
Product:         AUDAPACK
Component:       AUDAPACK Bridge
Scheduled Task:  AUDAPACK Bridge
Source root:     V:\___VAC\__K\__CODE\_PY\_AUDAPACK
Local state:     %LOCALAPPDATA%\AUDAPACK
Default port:    127.0.0.1:17843
```

Preferred code entry point:

```text
V:\___VAC\__K\__CODE\_PY\_AUDAPACK\AUDAPACK.pyw --bridge
```

Equivalent module entry point is acceptable if cleaner, but the Scheduled Task must ultimately point to the AUDAPACK tree, not to `_AICHATBUTTONS` and not to `%LOCALAPPDATA%\ACBBridge\app`.

A small stable launcher such as:

```text
START_AUDAPACK_BRIDGE.vbs
```

is acceptable if it lives inside the AUDAPACK root and invokes the canonical bridge entry point without a console flash.

---

## G1. Separate source from mutable state

Code lives in:

```text
V:\___VAC\__K\__CODE\_PY\_AUDAPACK
```

Mutable machine/user data lives in:

```text
%LOCALAPPDATA%\AUDAPACK\
    config.json or bridge-state equivalent
    token.txt or protected token equivalent
    logs\
    state\
    migration\
```

Do not copy the whole bridge source into LocalAppData as the production app runtime unless there is a demonstrated technical requirement.

The user explicitly wants the repaired installation to launch from the new AUDAPACK project location.

---

## G2. Detect legacy state

Implement a deterministic legacy detector that checks:

1. Scheduled Task `ACBBridge`;
2. `%LOCALAPPDATA%\ACBBridge`;
3. `%LOCALAPPDATA%\ACBBridge\config.json`;
4. `%LOCALAPPDATA%\ACBBridge\token.txt`;
5. `%LOCALAPPDATA%\ACBBridge\bridge.pid`;
6. current health response on `127.0.0.1:17843`;
7. where the running process actually came from, when safely inspectable.

Surface a migration status such as:

```text
LEGACY ACBBridge DETECTED
READY TO MIGRATE
```

Do not treat a random process on port 17843 as legacy ACBBridge merely because the port matches.

---

## G3. Preserve compatible token/settings

Default migration behavior should preserve the current bridge token where safe so the browser widget does not lose connectivity during takeover.

Migrate recognized settings only:

- loopback host;
- port;
- output/audit root;
- request size limit;
- relevant history settings;
- token;
- compatible persistent run state if required for undelivered work.

Do not blindly copy arbitrary legacy config keys into the new schema.

Normalize and validate.

If the legacy token is malformed or absent, generate a new high-entropy token and mark the Widget configuration as needing update/reconnect.

---

## G4. Migration backup

Before destructive cleanup, create a bounded migration backup/record under:

```text
%LOCALAPPDATA%\AUDAPACK\migration\ACBBridge_<timestamp>\
```

Include only useful state/config metadata needed for rollback or diagnosis.

Do not duplicate giant source trees.

At minimum preserve copies of existing:

- config;
- token metadata/file if policy permits;
- run-state files required for undelivered audit recovery;
- task definition/export if practical;
- migration report.

Never print token content into logs or migration report.

---

## G5. Safe old-process ownership validation

Preferred stop sequence:

1. verify legacy health/identity if endpoint exposes enough evidence;
2. use the legacy authenticated shutdown endpoint with the legacy token if valid;
3. stop Scheduled Task `ACBBridge`;
4. wait and verify port/process state;
5. only use PID-file termination if the PID still exists AND its executable/command line is demonstrably the legacy ACBBridge runtime;
6. never kill an unrelated process solely because it owns port 17843.

If ownership cannot be proven, fail with a clear diagnostic instead of force-killing.

---

## G6. Transactional takeover order

Migration must follow this sequence:

### Phase A — prepare

- validate AUDAPACK source root;
- validate Python/pythonw launcher;
- create `%LOCALAPPDATA%\AUDAPACK` state;
- migrate/normalize config/token;
- prepare new bridge task definition but do not create a competing live server yet.

### Phase B — stop old owner

- stop legacy bridge safely;
- verify `ACBBridge` task is no longer running;
- verify port 17843 is free or owned by the expected migration process only.

### Phase C — start new owner

- start AUDAPACK Bridge manually/ephemerally first;
- verify `/health`;
- health must identify itself as AUDAPACK Bridge and report compatible API/version;
- verify token auth;
- verify writable state/audit root contract.

### Phase D — install new autostart

Register:

```text
Scheduled Task: AUDAPACK Bridge
```

Action must point to the canonical AUDAPACK root/launcher.

Recommended settings:

- current interactive user;
- at logon;
- StartWhenAvailable;
- allow on battery;
- no arbitrary execution timeout;
- bounded restart policy;
- no administrator requirement for normal install.

### Phase E — remove legacy autostart

Only after the new task + bridge health check pass:

- unregister old Scheduled Task `ACBBridge`;
- ensure it cannot restart at next login;
- remove only the obsolete copied app runtime under `%LOCALAPPDATA%\ACBBridge\app` if safe;
- do not purge legacy state irreversibly unless explicitly requested or backup is complete.

### Phase F — verify reboot/logon contract

Stop the new bridge, start it through the new Scheduled Task, and verify health again.

The resulting live process must trace back to the AUDAPACK source/launcher, not the old AICHATBUTTONS bridge tree.

---

## G7. Idempotent Repair action

AUDAPACK GUI must expose a repair/migrate operation that is safe to run repeatedly.

Expected states:

```text
NOT INSTALLED
LEGACY DETECTED
MIGRATION REQUIRED
AUDAPACK INSTALLED
AUDAPACK RUNNING
AUDAPACK ERROR
PORT CONFLICT
```

Running Repair twice must not create duplicate tasks or regenerate token unnecessarily.

If the old `ACBBridge` task reappears, repair detects and neutralizes the conflict after verifying ownership.

---

## G8. Do not delete the supplied reference snapshot

If the working AUDAPACK tree contains a copied `_AICHATBUTTONS` directory, it is intentional reference material supplied by the user.

Rules:

- keep it read-only during migration work;
- do not execute its `INSTALL.cmd` as part of the new install;
- do not use its ACBBridge as production runtime;
- exclude it from normal AUDAPACK release/package output;
- use it for tests/comparison only.

---

## Tests

Automate what can be automated and provide Windows smoke for the rest.

Required cases:

1. no legacy installation;
2. legacy task only;
3. legacy LocalAppData only;
4. full legacy install;
5. legacy bridge currently running;
6. port 17843 occupied by unrelated process;
7. valid legacy token preserved;
8. invalid token regenerated;
9. malformed legacy config;
10. migration run twice;
11. new task points to AUDAPACK path;
12. old task removed only after new health succeeds;
13. simulated new bridge startup failure leaves recoverable legacy state;
14. PID mismatch does not kill unrelated process;
15. repair after partial migration;
16. new Scheduled Task starts the bridge successfully;
17. legacy `%LOCALAPPDATA%\ACBBridge\app` is no longer the live runtime.

---

## Required migration report

Produce a concise report containing:

```text
LEGACY ACBBridge: FOUND / NOT FOUND
OLD TASK: ...
OLD RUNTIME: ...
TOKEN: PRESERVED / REGENERATED
NEW SOURCE ROOT: ...
NEW STATE ROOT: ...
NEW TASK: ...
PORT: ...
HEALTH: PASS / FAIL
OLD TASK REMOVED: YES / NO
ROLLBACK DATA: ...
```

Never print the actual token.

---

## Acceptance gate

Wave G passes only if the old `ACBBridge` installation can no longer autostart as a competing owner, the new `AUDAPACK Bridge` autostarts from the canonical AUDAPACK tree, compatible state/token is preserved safely, and a verified health check proves the new owner is running.

Do not proceed to routed audit storage while two bridge owners can still exist.
