# AUDAPACK — LIVE 6-WORKER / SAVE RECOVERY: CURRENT TRUTH

Source snapshot:
- `_AUDAPACK_01.09.26-T08-21-56.zip`
- SHA-256: `2513a13222b266528e022cd4cd510e125b5cc10fa3e8bd694cc5b0904d5063bd`
- Git branch: `main`
- Git HEAD: `d46fae828997aeb69ea5e2260322a76e8951478c`
- AUDAPACK version: `0.2.2`
- bundled Widget `@version`: `0.0.24`
- browser worker protocol: `AUDAPACK_WIDGET/3`

## User-observed production symptoms

The live screenshot shows:
- 6 audit lanes displayed;
- three projects actively at `AUDIT 0/3`;
- one project at `WAITING FOR WORKER`;
- two projects at `BLOCKED POST-START`;
- Bridge footer around `W 3/6` while more Chromium windows are visibly open;
- two fresh-looking worker windows exist but are not contributing usable worker capacity.

The supplied Bridge diagnostics from the live machine show three save failures:

`completed_wave_immutable`

for:
- core
- second
- performance

of VACZEN Calendar.

Those diagnostics explicitly came from Widget `0.0.22`.

The CURRENT source bundle is Widget `0.0.24`.

That discrepancy is critical.

## Verified conclusion #1 — production runtime skew is real

The current source contains a T83 compatibility branch:

`completed_wave_immutable` is treated as "already durably complete", the job is removed, and no `job_failed` event is emitted.

The supplied live diagnostics instead show:
- permanent FAILED save jobs;
- `Bridge rejected an idempotency receipt conflict`.

Therefore the browser is not executing the current bundled save logic.

The installed/running Widget is stale.

## Verified conclusion #2 — Bridge cannot identify a stale Widget build

Worker heartbeat currently reports:
- `widget_version = AUDAPACK_WIDGET/3`

That is a PROTOCOL version, not the userscript `@version`.

A stale `0.0.22` and current `0.0.24` can therefore both present the same protocol identity and be accepted by the dispatcher.

The Bridge has no trustworthy way to say:
`this worker is running an old patch`.

## Verified conclusion #3 — app/Bridge build identity is also too weak

AUDAPACK remains `0.2.2` across many reliability fixes.

`/health` exposes semantic version but no source/build fingerprint.

A running old Bridge and the current source tree can both report:
`server=0.2.2`.

The operator cannot prove which code is actually running.

## Verified conclusion #4 — a durable finished audit can remain BLOCKED forever

`BrowserDispatcher.complete_for_run()` currently marks COMPLETE only when the matching job is:

- AUDITING
- FINALIZING

It excludes `BLOCKED`.

Mechanical reproduction against the current source:

1. run reaches AUDITING;
2. dispatcher restart converts it to:
   `BLOCKED`, recovery_state=`AUDITING`;
3. durable `campaign.json` + final handoff are created;
4. `complete_for_run(...)` returns `None`;
5. dispatch remains `BLOCKED`.

This directly explains the class of symptom:
`audit files are actually complete/saved, but Audit Runs still says BLOCKED POST-START and never becomes AUDIT READY`.

The final-save request currently ignores a `None` return from `complete_for_run`, so disk success can coexist with a permanently blocked transport lane.

## Verified conclusion #5 — visible browser windows != registered usable workers

The screenshot visually shows more Chromium windows than the Bridge reports as workers.

The normal UI does not tell the operator, per managed slot:

- was the process launched?
- did the Widget load?
- which Widget build is running?
- did it heartbeat?
- is it CLEAN?
- is it stale?
- is there a duplicate window for the same slot?
- why can it not claim?

This makes `W 3/6` impossible to diagnose from the main screen.

## Current source already contains useful repairs

Preserve:
- managed slot identities;
- unique per-window autoTabId suffix;
- six-lane provisioning;
- Bridge-side dispatch supervisor;
- bounded spawn budget;
- clean-chat proof;
- abandoned-draft cleanup;
- dirty worker recycle watchdog;
- stale local lease cleanup;
- post-START same-worker reconciliation;
- T83 client-side handling of `completed_wave_immutable`;
- exactly-once START fencing.

Do not rebuild these.

## Observed verification

Widget:
- 190 / 190 PASS

Focused Python:
- 129 PASS
- 1 real regression FAIL:
  `test_diagnostics_redacts_paths_tokens_and_content`
- 3 Qt import failures caused by missing PySide6 in the inspection environment.

HTTP dispatcher test group still hits production long-poll waits in tests where immediate state was intended.

## Core repair target

Make the running system self-identifying and self-healing.

A healthy six-audit session should prove:

`local source build == Bridge build == worker Widget build`

then:

`6 requested runs -> 6 commissioned managed slots -> all usable/diagnosable -> all audits durably save -> BLOCKED completed runs reconcile -> AUDIT READY`.

No hidden stale 0.0.22 worker may participate.
