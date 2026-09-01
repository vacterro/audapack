# IMMEDIATE RECOVERY FOR THE CURRENT LIVE MACHINE

This is an operator recovery procedure, not the final architectural fix.

## 1. Stop starting more audits temporarily

Do not repeatedly press START while the old `0.0.22` Widget is still running.

Repeated starts make it harder to separate:
- old blocked lineages;
- current jobs;
- stale save queue records.

## 2. Update the Widget in the dedicated AUDAPACK Chromium profile

Current bundle:
`0.0.24`

Live diagnostics:
`0.0.22`

Use:
`Settings -> Install Widget in AUDAPACK Chromium`

Confirm the userscript manager actually replaces the installed script.

Do not rely on the app version `0.2.2` as proof of Widget freshness.

## 3. Reload every managed worker page after Widget update

Existing pages continue executing the old userscript until reloaded.

All managed windows must reload/reopen after the update.

## 4. Restart the Bridge from the current workspace

Use:
`Settings -> Restart Bridge`

The Bridge still reports `0.2.2`, so semantic version alone does NOT prove a current build.
This roadmap adds a build fingerprint later.

## 5. Do not blindly FORCE UNBLOCK a post-START run

For VACZEN the provided diagnostics show the canonical run already saved all three waves, so it is a strong candidate for automatic durable reconciliation.

For any other blocked run:
- first verify whether its final handoff/campaign is durably complete;
- if complete, it should be reconciled to READY by the implementation fix;
- if incomplete, keep recovery lineage.

Do not erase a possibly running post-START audit simply to free a lane.

## 6. Re-test with a small batch first

After update/reload/restart:
- start 2 projects;
- verify Widget build and Bridge worker registration;
- then test all 6.

The final implementation must make these manual checks unnecessary.
