# Canonical Widget regression suites

These Node test suites protect the mature widget behavior inherited from the
legacy AICHATBUTTONS implementation and now target the single canonical
browser component:

    resources/AUDAPACK_WIDGET.user.js

Run one suite:

    node tests/widget/w2-001-lease.test.js

Run all:

    Get-ChildItem tests/widget -Filter *.test.js | ForEach-Object { node $_.FullName }

## Baseline parity note (Wave K / UI-K2)

At migration time the suites were executed against BOTH the legacy
`AICHATBUTTONS.js` and the canonical `AUDAPACK_WIDGET.user.js`. Results were
identical (7 pass / 5 fail on each source). The 5 failing suites below are
inherited baseline reds, NOT regressions introduced by AUDAPACK:

- perf-002-observer.test.js
- perf-005-snapshot.test.js
- w2-004-classify.test.js
- w2-005-lineage.test.js
- w2-006-observer.test.js

Migration invariant: AUDAPACK must never add new failures relative to this
recorded baseline. When one of these is repaired, remove it from this list.
