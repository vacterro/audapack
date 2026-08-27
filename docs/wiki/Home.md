# AUDAPACK Documentation Wiki

Welcome to the comprehensive technical documentation and architecture reference for **AUDAPACK**.

---

## 📑 Table of Contents

1. 🏠 **[Home & System Overview](Home.md)**
   - Mission statement, core philosophy, and high-velocity workflow.
2. 🔌 **[Architecture & Bridge Daemon](Architecture-and-Bridge.md)**
   - Process topology, HTTP API v2 specifications, token security, and endpoint contracts.
3. 🤖 **[Audit Campaign Engine & Super10 Deep Audit](Audit-Campaign-Engine.md)**
   - Generic $N$-wave data-driven audit campaigns, Super10 10-wave execution, Bridge API v3, lineage verification, and automated synthesis.
4. 🤖 **[Auto3 Audit Automation Pipeline (Legacy)](Auto3-Audit-Pipeline.md)**
   - 3-wave audit sequence (Core, Second Wave, Performance), Tampermonkey userscript state machine, and `runId` lease boundaries.
5. 🎨 **[Golden Vintage UI Design System](UI-Golden-Vintage.md)**
   - Windows 95 Dark Golden palette, 2px physical depth bevels, and zero-antialiasing typography rules.
6. 📦 **[CLI & Silent Packaging Automation](CLI-and-Silent-Packaging.md)**
   - Command-line arguments, silent background packaging, Windows Explorer context menu hooks, and CRC validation.

---

## 🎯 Design Philosophy

AUDAPACK was engineered around three non-negotiable principles:

1. **Extreme Operational Speed**:
   Developers should package projects, run AI audits, and hand off canonical context in single-click actions taking under 100 milliseconds.
2. **Total Reliability & Atomic Integrity**:
   No partial files, no corrupted ZIP archives, and no stale audit data. All write operations use two-phase staging (`.part` files + hash checks).
3. **Fail-Closed Privacy**:
   All communication occurs over loopback (`127.0.0.1`). Bridge authentication tokens reside in protected user application data and are never exposed in public repositories.
