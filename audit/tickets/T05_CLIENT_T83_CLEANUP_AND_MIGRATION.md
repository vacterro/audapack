# T05 — P0: CLEAN OLD 0.0.22 FAILED SAVE QUEUE

## Current source

0.0.24 already treats `completed_wave_immutable` as successful end-state.

Extend this into an upgrade migration.

## On Widget startup / Bridge queue recovery

For permanent jobs from older builds with:
- `completed_wave_immutable`
- materialize/save semantics

reconcile them.

If Bridge confirms canonical wave already exists:
- log `job_already_complete`;
- clear canonical record bridgeError/saveError;
- delete old queue job.

Do NOT retry forever.

## Bridge state

Historical resolved failures must not leave:
`bridgeState=error`

for the whole session.

Separate:
- Bridge health
- active save queue errors
- historical resolved errors

## Current branch polish

When handling `completed_wave_immutable` as success:
- explicitly set bridgeState to connected;
- set an honest message:
  `Already saved · canonical wave is complete`;
- update bridgeLastCheckedAt.

## Tests

- upgrade store containing the exact three VACZEN 0.0.22 failures becomes clean.
- no `failed 3` after reconciliation.
- no audit text is deleted.
