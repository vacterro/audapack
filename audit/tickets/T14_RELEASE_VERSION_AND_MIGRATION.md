# T14 — FINAL RELEASE / MIGRATION

## Versioning

Stop shipping many materially different runtime builds under the same visible identity.

At minimum:
- bump AUDAPACK patch when Bridge behavior changes;
- bump Widget @version when Widget behavior changes;
- expose build fingerprint regardless of semantic version.

## Upgrade migration

On first run after this repair:
- detect old 0.0.22/0.0.23 workers;
- surface update required;
- reconcile old completed_wave_immutable queue entries;
- reconcile durable-complete BLOCKED dispatches;
- preserve audit files and campaign history.

No destructive reset.

## Documentation

Primary troubleshooting:
1. APP / BRIDGE / WIDGET build match
2. six slot commissioning states
3. audit run state
4. save queue state

The operator should no longer need to paste a 100-line diagnostic merely to discover that two windows run an old userscript.
