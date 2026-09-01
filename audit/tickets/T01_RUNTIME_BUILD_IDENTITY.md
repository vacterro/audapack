# T01 — P0: RUNTIME BUILD IDENTITY

## Goal

Prove which code is actually running.

## Bridge build identity

Add to `/health` and `/v1/status`:
- app_version
- build_id
- source_revision when available
- widget_bundle_version
- widget_bundle_sha256
- browser_worker_protocol

`build_id` must change when relevant shipped source changes.

Good options:
- Git commit + dirty suffix;
- deterministic package/source manifest hash.

Do not expose filesystem secrets.

## Desktop

Settings and diagnostics show:

`APP 0.2.2 build abc123`
`BRIDGE 0.2.2 build abc123`
`WIDGET bundled 0.0.24 sha ...`

If APP != BRIDGE:
show:
`BRIDGE OUTDATED · RESTART REQUIRED`

Do not merely show green because semantic versions match.

## Tests

- same semantic version / different build -> mismatch detected.
- restart on current source -> build match.
- diagnostics include build id.
