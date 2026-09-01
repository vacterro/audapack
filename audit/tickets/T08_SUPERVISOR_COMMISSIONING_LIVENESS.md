# T08 — P0: SUPERVISOR MUST CONVERGE TO CLAIMABLE CAPACITY

## Preserve

Current Bridge-side DispatchSupervisor, boot grace and bounded launch budget.

## Change the success metric

A launched process is not success.

A registered but stale/dirty worker is not spare capacity.

Pool demand must converge against:
`claimable managed workers`

not merely:
`windows launched` or `registered slots`.

## Per-slot commissioning

Launch:
`LAUNCHING`

Within deadline:
- current Widget heartbeat + matching slot/generation -> commissioned
- then CLEAN/LEASED/etc.

If deadline expires:
- NO_HEARTBEAT / STALE
- bounded relaunch/recycle path
- no infinite window spam.

## Demand

For runnable work:
- ensure enough commissioned slots up to 6.

Do not count old BLOCKED_POST_START attention lanes as runnable worker demand unless they still own/recover an active worker.

## Spawn storm guard

All retries:
- per slot
- bounded
- cooldown
- generation-safe

## Tests

- six queued jobs -> six commissioned slots.
- two launched windows never heartbeat -> clearly failed slots and bounded retries.
- three current workers + one queued -> fourth commissioned.
- stale workers do not satisfy demand.
