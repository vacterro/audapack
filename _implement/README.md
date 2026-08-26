# AUDAPACK — SAIPEN GG Implementation Pack v3

This directory contains the historical AUDAPACK implementation waves plus the current production-hardening continuation.

## Current project state

Root SAIPEN state is now initialized at:

```text
.saipen/
    STATE.md
    BOARD.md
    LOG.md
```

For the current implementation represented by:

```text
_AUDAPACK_26-08-2026-T04-22-28.zip
```

**do not restart Waves A-I.** They are historical design/implementation context.

The next executable wave is:

```text
13_WAVE_J_INTEGRATION_CORRECTNESS_PRODUCTION_CUTOVER.md
```

Then, only when Wave J gates are green:

```text
10_FINAL_ACCEPTANCE_AUDIT.md
```

Use `11_CHAIN_CONTROLLER.md` for continuation semantics.

## Canonical installation target

```text
V:\___VAC\__K\__CODE\_PY\_AUDAPACK
```

## Legacy bridge being replaced

```text
Source installer:
V:\___VAC\__K\__CODE\_TAMPERMONKEY\_AICHATBUTTONS\ACBBridge\INSTALL.cmd

Scheduled Task:
ACBBridge

Legacy runtime/state:
%LOCALAPPDATA%\ACBBridge

Default port:
127.0.0.1:17843
```

Target production owner:

```text
AUDAPACK Bridge
Scheduled Task: AUDAPACK Bridge
Source: V:\___VAC\__K\__CODE\_PY\_AUDAPACK
Mutable state/secrets: %LOCALAPPDATA%\AUDAPACK
```

The copied `_AICHATBUTTONS` tree inside the AUDAPACK working folder is intentional read-only reference material, not production runtime. Keep it for parity/architecture comparison if useful; exclude it from ordinary release packaging and pytest discovery.

## Target audit pipeline

```text
AUDAPACK Widget (full AICHATBUTTONS-derived behavior)
    -> detects project identity
    -> resolves stable project_id through AUDAPACK Bridge API v2
    -> existing project follows current registry placement
    -> unknown project atomically auto-registers in first free SIDE1+ slot
    -> completed audit persists through durable browser queue
    -> AUDAPACK Bridge validates exact wave structure
    -> Bridge re-resolves current registry at physical delivery time
    -> canonical audit files written under AUDITING_IMPLEMENTATION/<GROUP>/<PROJECT>
    -> ALL_3 only after strict valid 3/3 for one run/project_id
    -> GUI updates temperature/readiness/NEW/COPIED
```

The browser never owns physical Windows destination paths.

## Historical implementation order

1. `00_MASTER_CONTRACT.md`
2. `01_WAVE_A_BASELINE_REFACTOR.md`
3. `02_WAVE_B_PROJECT_REGISTRY_UI.md`
4. `03_WAVE_C_AUDIT_ROOM.md`
5. `04_WAVE_D_SAIPEN_AWARENESS.md`
6. `05_WAVE_E_CONTEXT_MENU_AND_PACKING.md`
7. `06_WAVE_F_BROWSER_WIDGET_MIGRATION.md`
8. `07_WAVE_G_LEGACY_ACBBRIDGE_TAKEOVER.md`
9. `08_WAVE_H_ROUTED_AUDIT_PIPELINE.md`
10. `09_WAVE_I_HARDENING_RELEASE.md`
11. `13_WAVE_J_INTEGRATION_CORRECTNESS_PRODUCTION_CUTOVER.md`
12. `10_FINAL_ACCEPTANCE_AUDIT.md`

`12_LEGACY_ACBBRIDGE_EVIDENCE.md` is reference evidence, not an execution wave.

## Current Wave J focus

Wave J is deliberately not a feature wave. It closes verified remaining seams:

- path containment;
- secret-free portable config and token rotation;
- transactional ACBBridge takeover;
- cross-process registry transaction;
- strict wave validation;
- project_id/run ownership and collision-safe state;
- atomic generation notification;
- Widget API v2 registry handshake;
- real Tampermonkey migration;
- pytest/reference isolation and platform-safe clipboard import;
- one production launcher/runtime surface;
- real Windows end-to-end acceptance.
