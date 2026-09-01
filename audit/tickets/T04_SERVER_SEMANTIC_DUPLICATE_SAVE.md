# T04 — P0: SERVER-SIDE SEMANTIC IDEMPOTENCE FOR COMPLETED WAVES

## Goal

An already-saved identical wave must never become a red data-loss error just because a fresh receipt was used.

## Current rule

Same receipt + same hash:
`200 duplicate`

Different receipt + completed wave:
`409 completed_wave_immutable`

## New rule

If wave is already complete:

A. incoming normalized content hash matches durable wave hash:
- return `200`
- `duplicate=true`
- return canonical run_id
- return existing written file paths
- return campaign_ready/final handoff status
- do not mutate the immutable wave.

B. content differs:
- keep `409 completed_wave_immutable`.

Immutability remains strict.

## Legacy materialize compatibility

For known legacy Widget materialization where:
- payload run_id uses an `acb-mat-*` delivery identity;
- content contains an existing canonical CAMPAIGN_RUN_ID;
- project/profile/wave match;

do NOT create a new synthetic campaign.

Resolve the existing canonical run and apply the same identical-content duplicate logic.

Keep this compatibility path narrow and tested.

## Why

This makes the Bridge robust even when an older browser client is temporarily present.

Client correctness is still required, but server safety no longer depends on perfect client patch freshness.

## Tests

- fresh receipt + same completed content -> 200 duplicate.
- fresh receipt + changed content -> 409 immutable.
- legacy acb-mat + canonical content run -> existing canonical duplicate.
- no second campaign created.
