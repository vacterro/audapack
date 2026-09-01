# T06 — P0: DURABLE COMPLETE MUST CLOSE A BLOCKED POST-START DISPATCH

## Verified current bug

`complete_for_run()` considers only:
- AUDITING
- FINALIZING

A post-start job in BLOCKED is ignored even when:
- project matches;
- campaign_run_id matches;
- campaign.json is COMPLETE;
- all waves are complete;
- final handoff exists and hashes correctly.

## Fix

Allow `complete_for_run()` to reconcile a BLOCKED job ONLY when:

- job.recovery_state is one of:
  START_PREPARED / STARTED / AUDITING / FINALIZING
- project_id matches exactly
- campaign_run_id matches exactly
- durable campaign proof passes
- final handoff proof passes

Then:
- state -> COMPLETE
- preserve recovery/error history for diagnostics
- set final handoff path/hash
- release worker/lease
- update generation
- wake queue.

This is safe because disk completion is stronger evidence than transport uncertainty.

## Server

When final wave persistence calls `complete_for_run()`:
- if a matching blocked lineage exists, it must become COMPLETE.
- if no dispatch exists, normal disk save still succeeds.

## Startup reconciliation

Also add a bounded pass that scans BLOCKED post-start jobs against durable campaign index and closes those already proven complete.

This repairs old stuck lanes without requiring FORCE UNBLOCK.

## Tests

Reproduce exactly:
- AUDITING
- restart -> BLOCKED recovery_state=AUDITING
- write valid complete campaign + handoff
- complete_for_run -> COMPLETE

Also test:
- wrong run id remains BLOCKED
- incomplete campaign remains BLOCKED
- missing handoff remains BLOCKED
