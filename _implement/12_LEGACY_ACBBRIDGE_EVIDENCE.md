# LEGACY ACBBridge — INSPECTED INSTALLATION EVIDENCE

This file records concrete behavior observed in the supplied AICHATBUTTONS/ACBBridge reference so the migration agent does not infer or reinvent the legacy installation contract.

## Source inspected

```text
_AICHATBUTTONS\ACBBridge\INSTALL.cmd
_AICHATBUTTONS\ACBBridge\scripts\install.ps1
_AICHATBUTTONS\ACBBridge\scripts\start.ps1
_AICHATBUTTONS\ACBBridge\scripts\uninstall.ps1
_AICHATBUTTONS\ACBBridge\config.default.json
```

User-reported live source location:

```text
V:\___VAC\__K\__CODE\_TAMPERMONKEY\_AICHATBUTTONS\ACBBridge\INSTALL.cmd
```

## INSTALL.cmd

`INSTALL.cmd` launches the PowerShell installer in its `scripts` directory.

## Legacy local layout

The PowerShell installer uses:

```text
%LOCALAPPDATA%\ACBBridge
%LOCALAPPDATA%\ACBBridge\app
%LOCALAPPDATA%\ACBBridge\logs
%LOCALAPPDATA%\ACBBridge\state\runs
%LOCALAPPDATA%\ACBBridge\config.json
%LOCALAPPDATA%\ACBBridge\token.txt
```

It recursively copies the ACBBridge source directory into:

```text
%LOCALAPPDATA%\ACBBridge\app
```

Therefore the installed runtime is not necessarily executing directly from the V: source tree.

## Python launcher

Installer attempts to resolve `pythonw.exe`; if unavailable it falls back toward `python.exe`.

The Scheduled Task action executes the copied bridge script:

```text
%LOCALAPPDATA%\ACBBridge\app\acbbridge.py
```

## Scheduled Task

Exact legacy task name:

```text
ACBBridge
```

Trigger:

```text
At logon
```

The installer starts this task immediately after registration.

## Health and port

Default config uses:

```text
host = 127.0.0.1
port = 17843
```

The installer polls:

```text
http://127.0.0.1:17843/health
```

for successful startup.

## Token

After successful installation the installer reads:

```text
%LOCALAPPDATA%\ACBBridge\token.txt
```

and copies the token to the clipboard for AICHATBUTTONS configuration.

The new migration should therefore preserve a valid token when practical to minimize browser-side disruption.

## Legacy default audit root

The supplied default config points to:

```text
V:\___VAC\__K\__CODE\_AI_STUFF_AGENTIC\__TO_AUDIT\AUDITING_IMPLEMENTATION
```

Legacy layout is effectively project-subdir based. The new AUDAPACK routing must supersede this with canonical registry-aware placement:

```text
<AuditRoot>\MAIN0\<Project>
<AuditRoot>\MAIN1\<Project>
<AuditRoot>\SIDE0\<Project>
<AuditRoot>\SIDE1\<Project>
```

## Legacy uninstall behavior to improve

The supplied uninstaller:

1. stops/unregisters Scheduled Task `ACBBridge`;
2. attempts authenticated `/v1/shutdown` using legacy token;
3. reads `%LOCALAPPDATA%\ACBBridge\bridge.pid`;
4. may force-stop that PID;
5. optionally purges all local state, otherwise removes copied `app` runtime.

The AUDAPACK migration must be stricter before force-stopping a PID: verify that the PID/command line belongs to the expected legacy bridge. Never terminate an unrelated process solely because a stale PID file or port number matches.

## Canonical target after migration

```text
Source/code:
V:\___VAC\__K\__CODE\_PY\_AUDAPACK

Scheduled Task:
AUDAPACK Bridge

Mutable local state:
%LOCALAPPDATA%\AUDAPACK

Default loopback endpoint:
127.0.0.1:17843
```

This evidence is a migration input, not a requirement to clone the old installation architecture.
