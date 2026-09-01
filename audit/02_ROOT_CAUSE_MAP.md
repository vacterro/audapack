# ROOT CAUSE MAP

## Save failure family

### Symptom
`Bridge rejected an idempotency receipt conflict`
plus:
`completed_wave_immutable`

### Immediate cause
Old Widget build interprets the server's immutable-complete response as a permanent failure.

### Deeper cause
Runtime compatibility is based on protocol `AUDAPACK_WIDGET/3`, not actual Widget patch/build.

### Secondary server weakness
The server returns 409 for a completed wave with a new receipt even when the incoming content is semantically identical to the already-durable wave.

### Result
An old client can display data-loss failure for data already present on disk.

---

## BLOCKED even though audit is done

### Symptom
Audit has saved final content, lane remains:
`BLOCKED POST-START`

### Root cause
`complete_for_run()` ignores BLOCKED jobs.

Disk finalization cannot close a dispatch that entered recovery BLOCKED earlier.

### Result
Durable truth and transport truth diverge.

---

## Five visible windows, only W 3/6

### Symptom
Several Chromium windows are visible, but Bridge counts fewer active/usable workers.

### Root causes that are currently opaque
A window may be:
- launched but not registered;
- running stale Widget;
- duplicate slot/generation;
- non-root ChatGPT;
- DIRTY;
- holding stale lease;
- in recycle cooldown;
- not heartbeat-compatible.

The main UI exposes almost none of this per slot.

### Result
The operator sees "windows exist" but cannot know why work is not claimed.

---

## Why current version labels are insufficient

AUDAPACK:
`0.2.2`

Widget protocol:
`AUDAPACK_WIDGET/3`

Neither uniquely identifies the running code.

The system needs:
- source/build fingerprint;
- userscript build version;
- bundle SHA;
- required worker build compatibility.
