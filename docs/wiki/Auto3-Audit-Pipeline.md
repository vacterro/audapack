# Auto3 Audit Pipeline

## Overview
Auto3 automates the full 3-wave software audit sequence within ChatGPT:
1. **Wave 1 — Core Audit (`01_AUDIT_CORE.md`)**: Primary structural and functional audit.
2. **Wave 2 — Second Wave (`02_AUDIT_SECOND_WAVE.md`)**: Deep inspection, edge cases, and second-order bugs.
3. **Wave 3 — Performance & Stability (`03_AUDIT_PERFORMANCE.md`)**: Performance profiling, leak detection, and robustness.

## Run Isolation & Safety
- Each audit run is tagged with a unique `runId`.
- Auto-send dispatches full pointer/mouse sequences with `form.requestSubmit()` fallback.
- Active run boundary prevents cross-run wave badge bleed (fresh audit starts clean at 0/3).
