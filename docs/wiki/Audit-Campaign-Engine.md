# Audit Campaign Engine & Super10 Deep Audit

The **AUDAPACK Audit Campaign Engine** is a generic, data-driven multi-wave software audit orchestration system. It replaces fixed 3-wave branching with declarative profile manifests (`audapack/data/audit_profiles.json`), enabling arbitrary $N$-wave campaigns with durable execution, predecessor cryptographic verification, same-wave recovery, and automated multi-wave synthesis.

---

## 1. Core Concepts & Architecture

### Declarative Campaign Profiles
Profiles are defined in `audapack/data/audit_profiles.json` and mirrored in `audapack/campaign.py` and the Tampermonkey widget userscript (`resources/AUDAPACK_WIDGET.user.js`):
- **Super10 (`super10`)**: 10-wave comprehensive software audit culminating in an adversarial Red Team synthesis and root-cause ticket deduplication.
- **Quick3 (`quick3`)**: 3-wave classic triage audit (Core $\rightarrow$ Second Wave $\rightarrow$ Performance), fully backwards-compatible with legacy Auto3.

### Wave Definition Schema
Each wave declares:
- `id`: Machine identifier (e.g. `architecture`, `correctness`, `redteam`).
- `ordinal`: 1-based sequential wave index (1..$N$).
- `wave_header`: Human/AI readable wave title.
- `title`: Short display label.
- `objective`: Primary scope and inspection focus.
- `ticket_prefix`: Diagnostic code prefix (e.g. `ARCH`, `CORR`, `PERF`, `RED`).
- `ticket_fields`: Required fields per finding ticket (e.g. `EVIDENCE`, `DEFECT`, `REPAIR`, `VERIFY`).
- `status_line`: Formatted status header template.
- `done_marker`: Distinct terminal gate verification marker.
- `depends_on`: Explicit predecessor wave IDs that must be completed.
- `output_filename`: Canonical output markdown filename on disk.

---

## 2. Super10 Profile Specification

| Wave # | Wave ID | Title | Prefix | Primary Focus | Output File |
|---|---|---|---|---|---|
| **1** | `architecture` | Architecture / System Invariants | `ARCH` | Subsystem boundaries, invariants, routing, state topology, entry points | `Project__01_AUDIT_ARCHITECTURE.md` |
| **2** | `correctness` | Correctness / Logic Bugs | `CORR` | Edge cases, nil/overflow, concurrency, contract violations | `Project__02_AUDIT_CORRECTNESS.md` |
| **3** | `state` | State / Data Integrity | `DATA` | Schemas, serialization, race conditions, atomic transitions, idempotency | `Project__03_AUDIT_STATE.md` |
| **4** | `recovery` | Failure / Recovery | `RECOV` | Timeout ladders, backoff, circuit breakers, panic/crash traps, restarts | `Project__04_AUDIT_RECOVERY.md` |
| **5** | `security` | Security / Auth | `SEC` | Auth boundaries, secret leakage, injection, path traversal, untrusted input | `Project__05_AUDIT_SECURITY.md` |
| **6** | `integration` | Integration / APIs | `INT` | IPC, protocol mismatches, payload drift, third-party error codes | `Project__06_AUDIT_INTEGRATION.md` |
| **7** | `verification` | Tests / Contracts | `TEST` | Untested edge paths, assertion quality, test gaps, false-positive tests | `Project__07_AUDIT_VERIFICATION.md` |
| **8** | `performance` | Performance / Stability | `PERF` | O(N) traps, allocations, contention, connection churn, memory leaks | `Project__08_AUDIT_PERFORMANCE.md` |
| **9** | `operator` | UX / Operator Experience | `UX` | Error messages, observability, CLI ergonomics, debug signals | `Project__09_AUDIT_OPERATOR.md` |
| **10** | `redteam` | Red Team / Synthesis | `RED` | Multi-failure chaining, adversarial attack trees, final root-cause deduplication | `Project__10_AUDIT_REDTEAM.md` |

---

## 3. Bridge API v3 Contract

The AUDAPACK Bridge Daemon exposes endpoints supporting both v3 and v2 schemas:

### Endpoint: `POST /v1/audits`
Payload Schema (v3):
```json
{
  "api_version": 3,
  "profile_id": "super10",
  "profile_version": "1.0.0",
  "manifest_hash": "0150e79661d7b03b2fee434b93ea0cec2ec584e2c68ebb375b7d41bfbd13ff87",
  "wave_id": "architecture",
  "wave_index": 1,
  "wave_count": 10,
  "predecessor_sha256": "NONE",
  "run_id": "a3-1724738400000-abcd",
  "conversation_id": "chat-uuid",
  "project_name": "MyProject",
  "project_id": "myproject-123456",
  "status": "complete",
  "completed_at": 1724738405000,
  "receipt": { ... },
  "content": "```markdown\nPROJECT_NAME: MyProject\n...\n```"
}
```

### Endpoint: `GET /v1/profiles`
Returns active manifest metadata and all supported profile definitions.

---

## 4. Artifact Layout & Campaign Synthesis

When a Super10 campaign finishes all 10 waves, AUDAPACK Bridge automatically synthesizes:
- `Project__00_SUPER_AUDIT_ALL.md`: Chronological concatenation of all 10 waves with manifest header.
- `Project__00_SUPER_AUDIT_FINAL.md`: Extraction of the Red Team deduplicated root-cause implementation handoff.
- `Project__00_SUPER_AUDIT_INDEX.json`: Machine-readable campaign index with hashes, timestamps, tickets, and execution metadata.
- `_history/{timestamp}_{run_hash}/`: Durable run-scoped historical archive of all wave artifacts and run manifest.

---

## 5. UI & State Integration

- **Qt Room Interface**: Real-time room badges render dynamic progress indicators `x/10` or `x/3`, turning gold when `campaign_complete = True`.
- **Tampermonkey Userscript**:
  - One-click profile selector (`S10` / `Q3`) on the widget header.
  - Settings panel dropdown for profile configuration.
  - Dynamic `1..N` interactive progress bubbles with direct clipboard copying of completed wave contents.
  - Same-wave partial continuation and unattended recovery loops.
