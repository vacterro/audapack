# T10 — P1: REMOVE SYNTHETIC MATERIALIZE RUN ID FROM CAMPAIGN IDENTITY

## Current code

Manual force-save still creates:
`acb-mat-*`

as `deliveryRunId`, while newer code correctly sends canonical `job.runId`.

This terminology is dangerous because earlier builds sent the synthetic id as real run_id.

## Refactor

Rename concept:
`delivery_batch_id`

Never call it run id.

Campaign identity remains:
`campaign_run_id`

Receipt may include delivery_batch_id for uniqueness.

## SAVE behavior

If canonical wave is complete:
- if files exist -> success/already saved;
- if files are missing/corrupt -> repair materialization from canonical run state;
- never create replacement campaign solely to rewrite files.

## Tests

No path in Widget or Bridge may use delivery_batch_id as campaign run_id.
