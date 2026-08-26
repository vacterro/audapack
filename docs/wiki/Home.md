# AUDAPACK Documentation Wiki

Welcome to the **AUDAPACK** documentation wiki.

## Overview
AUDAPACK is an ultra-fast, lightweight Windows desktop utility, audit room cockpit, and browser automation bridge designed for high-velocity software engineering.

## Architecture
- **24-Slot Priority Room**: 4 groups (`MAIN0`, `MAIN1`, `SIDE0`, `SIDE1`), 6 slots each.
- **Audit Temperature & Freshness**: Real-time tracking (`0/3`, `1/3`, `2/3`, `3/3`, `HOT`, `WARM`, `COOL`, `COLD`, `STALE`).
- **Clean ZIP Packaging**: Staging via `.part`, mandatory excludes, manifest generation, and CRC validation.
- **Tampermonkey Automation**: Browser userscript (`AUDAPACK_WIDGET.user.js`) automating 3-wave audits with runId boundary isolation.
- **AUDAPACK Bridge**: Loopback HTTP daemon on `127.0.0.1:17843` (API v2).
