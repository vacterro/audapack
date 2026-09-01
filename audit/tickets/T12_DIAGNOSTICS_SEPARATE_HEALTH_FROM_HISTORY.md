# T12 — P1: DIAGNOSTICS THAT DO NOT LIE

## Current user diagnostic

`state=error`
because three historical save jobs failed.

That can coexist with:
`Bridge connected and current audits working`.

Split:

BRIDGE:
- connected/offline/auth

SAVE QUEUE:
- pending
- active failed
- resolved historical

WORKERS:
- commissioned/claimable/stale/no-heartbeat

RUNS:
- running/waiting/attention/ready

## Historical errors

Resolved T83 immutable-complete records move to:
`RESOLVED`

They do not keep global state red.

## COPY DETAILS

Include:
- app build id
- Bridge build id
- Widget build per worker
- managed slot/generation
- exact current run ids
- queue state

No token/content.
