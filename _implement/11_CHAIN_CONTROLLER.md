# SAIPEN GG — CHAIN CONTROLLER v3

Load `00_MASTER_CONTRACT.md` for durable product invariants, then inspect root `.saipen/STATE.md`, `.saipen/BOARD.md`, and the tail of `.saipen/LOG.md`.

## Current continuation baseline

For the current implemented project at or after:

```text
_AUDAPACK_26-08-2026-T04-22-28.zip
```

Waves A-I are historical implementation context. Do **not** restart them from scratch.

The executable continuation is:

1. `13_WAVE_J_INTEGRATION_CORRECTNESS_PRODUCTION_CUTOVER.md`
2. `10_FINAL_ACCEPTANCE_AUDIT.md` only after Wave J production gates are green.

## Fresh-rebuild order only

If intentionally rebuilding from a pre-AUDAPACK baseline rather than continuing the current implementation, the historical order remains:

1. `01_WAVE_A_BASELINE_REFACTOR.md`
2. `02_WAVE_B_PROJECT_REGISTRY_UI.md`
3. `03_WAVE_C_AUDIT_ROOM.md`
4. `04_WAVE_D_SAIPEN_AWARENESS.md`
5. `05_WAVE_E_CONTEXT_MENU_AND_PACKING.md`
6. `06_WAVE_F_BROWSER_WIDGET_MIGRATION.md`
7. `07_WAVE_G_LEGACY_ACBBRIDGE_TAKEOVER.md`
8. `08_WAVE_H_ROUTED_AUDIT_PIPELINE.md`
9. `09_WAVE_I_HARDENING_RELEASE.md`
10. `13_WAVE_J_INTEGRATION_CORRECTNESS_PRODUCTION_CUTOVER.md`
11. `10_FINAL_ACCEPTANCE_AUDIT.md`

## Continuation protocol

After each checkpoint:

1. run affected focused tests;
2. run the full intended regression suite;
3. append meaningful evidence to root `.saipen/LOG.md`;
4. update root `.saipen/BOARD.md`;
5. update `.saipen/STATE.md` last with the next directly executable action;
6. preserve previously verified invariants;
7. continue only when the current gate is green.

If interrupted, resume from the last verified checkpoint of the same wave. Do not restart completed work.

If a later check exposes a root defect in earlier implementation, repair the root cause inside the current Wave J scope, rerun affected regression tests, and continue forward.

Critical sequencing rule:

```text
Never remove legacy Scheduled Task ACBBridge until AUDAPACK Bridge identity, authentication, registry, controlled write, new Scheduled Task command, and task-triggered startup are all verified.
```

The copied `_AICHATBUTTONS` folder inside AUDAPACK is read-only reference material. Do not mutate its `.saipen` as AUDAPACK state, do not delete it automatically, and do not use it as production runtime.

Final completion requires `10_FINAL_ACCEPTANCE_AUDIT.md` after Wave J.
