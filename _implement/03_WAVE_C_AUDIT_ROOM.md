# WAVE C — AUDIT INDEX, TEMPERATURE, READINESS, COPY AUDIT

## Goal

Make AUDAPACK understand the audit state of each project and turn the priority room into a useful audit cockpit.

---

## Canonical files

Per project:

```text
<Project>__01_AUDIT_CORE.md
<Project>__02_AUDIT_SECOND_WAVE.md
<Project>__03_AUDIT_PERFORMANCE.md
<Project>__00_AUDIT_ALL_3.md
_history\
```

Latest canonical files live in the project audit folder. `_history` is history, not the source of latest state unless recovery logic explicitly needs it.

---

## Required work

### C1. AuditSnapshot model

Build a computed snapshot such as:

```text
project
core_path
core_complete
second_path
second_complete
performance_path
performance_complete
all3_path
all3_ready
audit_timestamp
audit_age
temperature
all3_sha256
```

### C2. Completion validation

Do not mark a wave complete merely because a file exists.

Validate enough structure/status to reject:

- `.part`;
- zero-byte output;
- clearly truncated output;
- explicit incomplete/blocked terminal status where the audit contract says not complete.

Use actual formats from supplied audit files and latest audit instructions.

### C3. Readiness states

Display:

```text
0/3
1/3
2/3
3/3
ALL
```

`ALL` means canonical ALL_3 exists and is valid.

### C4. Temperature

Use deterministic timestamp precedence:

1. `GENERATED_AT` in latest ALL_3;
2. latest valid audit metadata `DATE_TIME`;
3. maximum timestamp among latest canonical waves;
4. filesystem mtime as fallback only.

Default thresholds:

```text
HOT      0–6 hours
WARM     >6–24 hours
COOL     >24–72 hours
COLD     >72 hours–7 days
STALE    >7 days
NONE     no audit
```

Thresholds must be centralized/configurable.

Show word + age:

```text
HOT · 2h 14m
WARM · 17h
COOL · 2d 3h
STALE · 11d
NONE
```

Do not communicate status by color alone.

Use only theme tokens, not random colors.

### C5. COPY AUDIT

Add button:

`КОПИРОВАТЬ АУДИТ`

It copies exact canonical ALL_3 text.

If ALL is not ready, disable button and show reason/state.

On click:

1. re-read file;
2. revalidate;
3. compute SHA-256;
4. copy exact decoded content;
5. persist:
   - `last_copied_audit_hash`
   - `last_copied_at`

### C6. COPIED / NEW logic

If current `all3_sha256 == last_copied_audit_hash`:

```text
✓ COPIED
```

Optional overstrike may be used in addition, never as the sole signal.

If a new ALL arrives with different hash:

- remove copied state;
- show `NEW`;
- enable copy again.

### C7. Refresh policy

Update audit state on:

- startup;
- explicit `Refresh Audits`;
- after known audit-file change in later bridge wave;
- after project registry edits.

Avoid aggressive disk polling.

### C8. Existing audits must index immediately

No new audit run should be required.

Current AUDITING_IMPLEMENTATION projects should show their latest state on first launch after migration.

---

## Tests

Fixtures:

- no audit;
- Core only;
- Core+Second;
- 3 waves;
- 3 waves+ALL;
- incomplete ALL;
- corrupt text;
- history only;
- stale metadata;
- mtime fallback.

Temperature boundary tests with injected clock:

- 5h59
- 6h00
- 6h01
- 23h59
- 24h
- 72h
- 7d
- >7d

Copy-state tests:

```text
ALL A → copy → ✓ COPIED
restart → still ✓ COPIED
ALL B → NEW
copy B → ✓ COPIED
```

Clipboard must equal the exact file text.

---

## Acceptance gate

Wave C passes only if project rows reliably show:

- readiness;
- age;
- temperature;
- ALL ready/not ready;
- copied/new state;

and exact COPY AUDIT works across restart and across a new ALL version.
