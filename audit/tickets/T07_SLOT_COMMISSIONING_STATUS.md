# T07 — P0: MAKE ALL SIX MANAGED SLOTS OBSERVABLE

## Problem

The screenshot shows more Chromium windows than Bridge worker count.

A visible window is not proof of a commissioned audit worker.

## Create a slot view

For slots 1..6 expose one canonical state:

- EMPTY
- LAUNCHING
- NO_HEARTBEAT
- STALE_WIDGET
- CLEAN
- LEASED
- AUDITING
- DIRTY
- OCCUPIED
- RECYCLING
- FAILED

Include:
- managed generation
- worker id short
- browser
- widget build
- last heartbeat age
- clean_for_audit
- claimable
- current project
- block reason

## Unique slot ownership

For a given:
`managed_slot + managed_generation`

there may be at most one PRIMARY active worker.

If duplicate windows register:
- choose/lock one primary safely;
- quarantine duplicates from claiming;
- expose `DUPLICATE SLOT`.

Do not let two windows flip the same slot between clean and busy.

## UI

Audit Runs header may show:

`SLOTS 5/6 · CLEAN 2 · RUN 3 · STALE 0 · NO HB 1`

A slot details popover tells the user exactly why a visible window is not counted.

## Gate

`W 3/6` can never be a mystery while five windows are visibly open.
