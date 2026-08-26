# Auto3 Audit Automation Pipeline

## 🚀 Overview

**Auto3** is the automated 3-wave software audit protocol executed by the Tampermonkey userscript (`AUDAPACK_WIDGET.user.js`) directly inside ChatGPT.

---

## 🌊 The 3 Canonical Waves

```
┌────────────────────────────────────────────────────────┐
│  Wave 1: Core Architecture (01_AUDIT_CORE.md)          │
│  • System decomposition & high-level component audit   │
│  • API contract adherence & invariant verification     │
└──────────────────────────┬─────────────────────────────┘
                           │ Trigger: Auto-continuation
┌──────────────────────────▼─────────────────────────────┐
│  Wave 2: Second Wave (02_AUDIT_SECOND_WAVE.md)         │
│  • Deep dive into edge cases & boundary conditions     │
│  • Concurrency hazards, race conditions & error paths  │
└──────────────────────────┬─────────────────────────────┘
                           │ Trigger: Auto-continuation
┌──────────────────────────▼─────────────────────────────┐
│  Wave 3: Performance & Stability (03_PERFORMANCE.md)   │
│  • Memory leak profiling & throughput optimization     │
│  • Conformance verification & regression benchmarks    │
└──────────────────────────┬─────────────────────────────┘
                           │ Delivery to Bridge
┌──────────────────────────▼─────────────────────────────┐
│  Synthesis: Master File (__00_AUDIT_ALL_3.md)          │
│  • Unified atomic handoff ready for 1-click clipboard  │
└────────────────────────────────────────────────────────┘
```

---

## 🛡️ Run Isolation & Lease Ownership

1. **Unique `runId` Tagging**:
   Every audit session generates a timestamped `runId`. All turns and wave payloads carry this identifier.
2. **Lease Management**:
   The userscript claims an ownership lease on the conversation composer before injecting prompt presets. If ownership is lost, execution halts safely.
3. **Simulated Pointer Dispatch**:
   The userscript dispatches real pointer/mouse event sequences (`pointerdown`, `mousedown`, `pointerup`, `mouseup`, `click`) to trigger submit buttons naturally, falling back to `form.requestSubmit()` when required.
4. **Clean Slate Boundary**:
   Starting a new audit run resets wave badges (`0/3`) immediately, preventing visual bleed from previous runs.
