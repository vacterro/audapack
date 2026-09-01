# T09 — P0: PRE-BATCH RECOVERY SWEEP

## Goal

Old runs must not silently occupy the six visible lanes when their durable truth is already known.

Before START GROUP / batch start:

For each existing BLOCKED_POST_START project:
1. check exact campaign lineage;
2. if durably COMPLETE -> reconcile to COMPLETE/READY;
3. if same worker is alive -> leave recovery active;
4. if unresolved -> keep ATTENTION and explain.

Do not force-unblock automatically when completion is not proven.

## Batch UI

Summary should distinguish:
- RUNNING
- WAITING
- ATTENTION
- READY

Do not label two BLOCKED attention runs as ordinary `active` capacity.

Example:
`RUN 3 · WAIT 1 · ATTENTION 2 · READY 0`

## Gate

A completed old VACZEN run cannot steal a lane from a fresh six-project batch.
