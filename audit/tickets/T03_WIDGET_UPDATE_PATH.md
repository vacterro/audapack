# T03 — P0: RELIABLE INSTALL / UPDATE PATH

## Goal

Updating the source tree must not leave six Chromium windows running an older userscript indefinitely.

## Bridge-served userscript

For `/widget.user.js`:
- add `Cache-Control: no-store, no-cache, must-revalidate`;
- add bundle ETag / SHA header;
- optionally add a query fingerprint to update URL.

## Settings

Rename:
`Install Widget in AUDAPACK Chromium`

to:
`INSTALL / UPDATE WIDGET`

Show:
- bundled version
- live worker versions
- update required count

## Update workflow

1. open the current Bridge-served userscript in the dedicated profile;
2. user confirms the userscript-manager update if required;
3. app waits for a worker heartbeat with the required build;
4. only then report:
   `WIDGET CURRENT`.

Do not report success merely because a browser window opened.

## Existing windows

After successful update:
- require reload/recycle of stale managed windows;
- verify their next heartbeat reports current build.

## Optional userscript metadata

Evaluate safe `@updateURL` / `@downloadURL` support using the localhost Bridge route.

Do not depend on it as the only update mechanism unless real Tampermonkey behavior is verified on Windows.

## Gate

It becomes impossible for a stale Widget to look green/current in AUDAPACK.
