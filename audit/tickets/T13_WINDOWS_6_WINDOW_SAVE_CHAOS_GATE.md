# T13 — RELEASE GATE: REAL WINDOWS SIX-WINDOW + SAVE TEST

This is the release proof for the user's actual failure.

## Phase 0 — prove runtime freshness

Before START:
- APP build id == Bridge build id
- bundled Widget 0.0.24/current
- every managed worker heartbeat reports required Widget build

If not:
test is invalid.

## Phase 1 — cold six

Close all managed workers.

START GROUP six projects.

Expected:
- six slot states become visible;
- six commissioned workers at most;
- no seventh window;
- every queued project gets a lane/worker as capacity permits.

## Phase 2 — observe exact claimability

If a window is visible but not counted:
the UI must show its exact state:
- NO_HEARTBEAT
- STALE_WIDGET
- DIRTY
etc.

No mystery W 3/6.

## Phase 3 — save all waves

For every project:
- Core durably saves
- Second durably saves
- Performance durably saves
- final handoff created
- dispatch -> COMPLETE
- UI -> AUDIT READY

No:
`completed_wave_immutable` red failure for identical already-saved content.

## Phase 4 — restart Bridge during AUDITING

Allow one run to enter BLOCKED recovery.

Then let the audit finish and save final handoff.

Expected:
durable completion auto-reconciles BLOCKED -> COMPLETE -> READY.

## Phase 5 — stale Widget simulation

Run one intentionally old worker build.

Expected:
- registered as STALE_WIDGET
- cannot claim
- exact update message
- current worker replaces its capacity.

## Phase 6 — second six

Run another six immediately.

No manual fresh-chat gardening.
No stale failed save queue.
No blocked completed lanes stealing capacity.

## Evidence

Record:
- app build
- Bridge build
- Widget builds
- six slot identities
- dispatch ids
- campaign run ids
- final paths/hashes
- elapsed time
- any retry/recycle events
