# Architecture & AUDAPACK Bridge

## System Topology
1. **Desktop App**: Native GUI in Python Tkinter and PySide6 Qt.
2. **Bridge Daemon**: HTTP micro-service running on `127.0.0.1:17843`.
3. **Tampermonkey Widget**: Userscript injected into ChatGPT.

## Endpoints (API v2)
- `GET /health` — Service handshake and health verification.
- `GET /v1/status` — Bridge status, active projects, and audit output verification.
- `POST /v1/audits` — Ingests audit waves, enforces runId boundaries, updates state, and generates `__00_AUDIT_ALL_3.md`.
- `GET /v1/registry` — Delivers registered project routes and current placement.

## Security & Isolation
- Bridge token stored in `%LOCALAPPDATA%\AUDAPACK\secrets\bridge_token.txt`.
- No credentials checked into source control or portable archives.
