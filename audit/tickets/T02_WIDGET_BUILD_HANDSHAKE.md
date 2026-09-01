# T02 — P0: SEPARATE WIDGET PROTOCOL FROM WIDGET BUILD

## Current defect

Worker heartbeat only reports:
`AUDAPACK_WIDGET/3`

A stale 0.0.22 worker and current 0.0.24 worker look identical.

## Add heartbeat fields

- `widget_protocol = AUDAPACK_WIDGET/3`
- `widget_build_version = 0.0.24`
- `widget_bundle_sha256` or short build hash
- optional `widget_capabilities`

Keep protocol compatibility separate from patch build.

## Bridge compatibility

For managed audit workers:
- stale/unknown build is NOT claimable;
- worker remains visible as:
  `STALE_WIDGET`
- do not silently use it for a new audit.

External/manual Widget functionality may remain less strict if required, but automatic managed audit dispatch must fail closed.

## Required build source

Bridge reads current bundled Widget metadata + SHA and exposes:
`required_widget_build`.

## UI

Per worker:
`Chrome slot 4 · STALE 0.0.22 -> need 0.0.24`

START should say:
`WAITING · UPDATE WIDGET`
instead of generic `WAITING FOR WORKER`.

## Tests

- current build claims.
- stale 0.0.22 registers but cannot claim.
- unknown build cannot claim managed audit.
- protocol mismatch remains separately diagnosed.
