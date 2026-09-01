# T11 — P1: START GROUP PREFLIGHT

Before starting six audits, perform one quick non-blocking preflight.

Report:
- Bridge build matches app build
- bundled Widget required version
- commissioned current managed slots
- stale workers
- output root writable

Do not require six workers before accepting the jobs.

Jobs may queue while slots start.

But if the only available managed workers are stale:
show:
`UPDATE WIDGET REQUIRED`

Do not send audit work to them.

## UX

START GROUP remains one action.

If update/setup is required:
- create no partial surprise batch unless policy explicitly says queued pending setup;
- show exact remedy.

## Gate

User knows before waiting 20 minutes that two windows are stale/unregistered.
