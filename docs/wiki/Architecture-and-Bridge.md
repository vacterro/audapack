# Architecture & AUDAPACK Bridge Daemon

## 🏛️ System Topology

AUDAPACK operates as a distributed local coordination system consisting of three cooperative layers:

```
┌───────────────────────────────────────────────────────────┐
│              🌐 Browser Context (ChatGPT Web)              │
│       AUDAPACK_WIDGET.user.js (Tampermonkey Userscript)   │
└─────────────────────────────┬─────────────────────────────┘
                              │ HTTP POST (API v2)
                              │ Loopback Token Auth
┌─────────────────────────────▼─────────────────────────────┐
│          🔌 Bridge Daemon (127.0.0.1:17843)               │
│   • Token validation   • Run boundary enforcement         │
│   • Wave ingestion     • __00_AUDIT_ALL_3.md aggregation  │
└─────────────────────────────┬─────────────────────────────┘
                              │ Local File Ingest & Signals
┌─────────────────────────────▼─────────────────────────────┐
│          🖥️ AUDAPACK Cockpit (Desktop Application)         │
│   • 24-Slot Priority Room   • Temperature calculation     │
│   • 1-Click Clipboard copy  • Atomic .zip packager        │
└───────────────────────────────────────────────────────────┘
```

---

## 📡 HTTP Bridge API (Version 2.0)

The bridge runs an asynchronous HTTP server listening strictly on `127.0.0.1:17843`.

### 1. Health Handshake (`GET /health`)
- **Purpose**: Verifies that the bridge daemon is responsive.
- **Auth**: None required.
- **Response**: `200 OK`
  ```json
  { "status": "ok", "version": "0.1.0", "api_version": 2 }
  ```

### 2. Registry Retrieval (`GET /v1/registry`)
- **Purpose**: Provides the browser widget with registered projects, priority positions, and path routes.
- **Headers**: `X-Bridge-Token: <token>`
- **Response**: `200 OK`
  ```json
  {
    "projects": [
      {
        "id": "AUDAPACK",
        "name": "_AUDAPACK",
        "path": "V:\___VAC\__K\__CODE\_PY\_AUDAPACK",
        "group": "MAIN0",
        "slot": 1,
        "enabled": true
      }
    ]
  }
  ```

### 3. Ingest Audit Wave (`POST /v1/audits`)
- **Purpose**: Delivers a completed wave (`01_AUDIT_CORE.md`, `02_AUDIT_SECOND_WAVE.md`, or `03_AUDIT_PERFORMANCE.md`) to the target project directory.
- **Headers**: `X-Bridge-Token: <token>`, `Content-Type: application/json`
- **Request Payload**:
  ```json
  {
    "project_id": "AUDAPACK",
    "run_id": "20260827_015500_abc123",
    "wave": "core",
    "filename": "01_AUDIT_CORE.md",
    "content": "# Audit Core Content...",
    "receipt": { "turn_id": "turn-42", "timestamp": "2026-08-27T01:55:00Z" }
  }
  ```
- **Lifecycle & ALL_3 Synthesis**:
  Upon receipt of the 3rd wave (`03_AUDIT_PERFORMANCE.md`), the bridge atomically synthesizes the unified master file `__00_AUDIT_ALL_3.md` and signals the UI to bump the generation index.

---

## 🔐 Security & Credential Isolation

- **Token Storage**: Bridge tokens are generated as 256-bit cryptographically secure random hexadecimal strings stored in:
  `%LOCALAPPDATA%\AUDAPACK\secrets\bridge_token.txt`
- **Zero-Exposure Policy**: Secrets are strictly excluded by `.gitignore` and never committed or packed into distributable archives.
