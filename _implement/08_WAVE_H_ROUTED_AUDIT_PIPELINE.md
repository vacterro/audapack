# WAVE H — REGISTRY-ROUTED AUDIT PIPELINE

## Goal

Complete the end-to-end pipeline so the fresh AUDAPACK Tampermonkey Widget still performs audits, but all completed audit waves are routed according to how projects are currently arranged in AUDAPACK:

```text
MAIN0
MAIN1
SIDE0
SIDE1
```

The browser sees logical placement. The bridge owns the physical filesystem routing.

---

## Canonical flow

```text
AI chat page
    -> AUDAPACK Widget
    -> audit Core / Second / Performance workflow
    -> durable browser queue
    -> AUDAPACK Bridge
    -> canonical Project Registry
    -> priority group + slot
    -> AUDITING_IMPLEMENTATION\<GROUP>\<PROJECT>
    -> canonical wave file
    -> canonical ALL_3 after valid 3/3
    -> AUDAPACK GUI invalidation/update
    -> COPY AUDIT / NEW / temperature
```

This is the primary acceptance flow for the integrated product.

---

## H1. One canonical project registry

The registry created in Wave B is the sole routing authority.

Each routable project has at least:

```text
project_id
display_name
source_path
priority_group
slot
audit_project_name
enabled
audit_enabled
```

Physical destination is derived by AUDAPACK, never accepted directly from the browser.

Example:

```text
project_id = fastprompter
priority_group = MAIN0
slot = 1
audit_project_name = FastPrompter
```

becomes:

```text
<configured audit root>\MAIN0\FastPrompter
```

---

## H2. Logical project registry endpoint

Expose a versioned authenticated bridge capability for the Widget, logically equivalent to:

```text
GET /v1/projects
```

Return only information useful for project resolution/UI, for example:

```json
{
  "registry_revision": "...",
  "projects": [
    {
      "id": "fastprompter",
      "name": "FastPrompter",
      "priority": "MAIN0",
      "slot": 1,
      "audit_enabled": true
    }
  ]
}
```

Do NOT expose:

- arbitrary source paths;
- audit root filesystem path;
- secrets;
- unrelated config.

Registry revision must change when routing-relevant assignments change.

---

## H3. Widget project resolution

The Widget keeps the existing project-name extraction logic, improved where needed.

Resolution order should be deterministic:

1. explicit stable project identity already attached to the audit run;
2. exact normalized match to `audit_project_name` / display name;
3. explicit configured aliases;
4. otherwise unresolved/ambiguous.

Do not fuzzy-match two similarly named projects into whichever happens to appear first.

Once a run accepts a project:

```text
run_id -> project_id
```

is immutable.

The Widget may display:

```text
AUDIT TARGET: FastPrompter · MAIN0 / 1
```

but the bridge re-validates it.

---

## H4. Unknown / ambiguous projects

Never lose completed audit content.

If Widget cannot resolve project:

- keep the delivery job durable;
- mark `PROJECT UNRESOLVED`;
- allow bridge submission by declared sanitized name only if the bridge's `_UNASSIGNED` contract supports it;
- never guess a MAIN/SIDE group.

Bridge may route unresolved valid audit to:

```text
<AuditRoot>\_UNASSIGNED\<SanitizedProject>
```

and expose it in AUDAPACK GUI for assignment.

`_UNASSIGNED` is a technical inbox, not a fifth priority group.

---

## H5. Wave submission contract

A completed audit wave submission must contain sufficient immutable identity, logically:

```text
api_version
run_id
wave
receipt
project_id or unresolved_project_name
content
content_sha256
source/chat metadata as non-authoritative context
```

Do not send destination path.

Do not trust browser-provided group/slot as authorization.

---

## H6. Bridge project routing

On each accepted wave:

1. authenticate;
2. validate request/schema;
3. load canonical registry snapshot/revision;
4. resolve `project_id`;
5. verify project is audit-routable;
6. bind run to project identity if first accepted wave;
7. reject same-run mismatch;
8. compute destination from current canonical assignment;
9. write transactionally.

Destination:

```text
<AuditRoot>\MAIN0\Project
<AuditRoot>\MAIN1\Project
<AuditRoot>\SIDE0\Project
<AuditRoot>\SIDE1\Project
```

---

## H7. Project moved between groups

Routing for **newly accepted runs** uses the current canonical registry assignment.

Do not silently merge or relocate historical folders during a bridge POST.

If a project is moved in AUDAPACK:

- subsequent new audit runs go to the new group;
- GUI should clearly detect old audit material in the former location if present;
- an explicit `Move/Reconcile Audit Folder` action may migrate existing canonical/history data after conflict checks.

Never let a stale Widget cache override the Bridge's current registry.

---

## H8. Per-run concurrency transaction

Production server may be threaded, but same-run writes serialize.

Lock scope:

1. load run state;
2. validate bound project;
3. validate receipt/content hash;
4. resolve canonical destination;
5. write wave atomically;
6. update run state atomically;
7. check 3/3 readiness;
8. generate canonical ALL_3;
9. publish ALL_3 atomically;
10. update final state;
11. return durable success.

Different runs may execute concurrently where safe.

---

## H9. Receipt and idempotency

For each:

```text
run_id + wave + receipt + content_sha256
```

Rules:

```text
same receipt + same content -> 200 duplicate=true
same receipt + different content -> 409 receipt_conflict
```

Conflict validation happens before evidence overwrite.

---

## H10. History identity

History path identity must derive from the full run identity using collision-resistant hashing or equivalent.

Never use only a short sanitized textual run-id prefix.

Different successful runs must not overwrite each other.

---

## H11. Config fails closed

If canonical Audit Root is missing, malformed, inaccessible or empty:

- do not write to current directory;
- do not return success;
- bridge reports retriable output/config error;
- Widget keeps job queued.

Last-known-good in-memory config is allowed only if deliberate and test-covered.

---

## H12. Canonical files

For each project directory:

```text
<Project>__01_AUDIT_CORE.md
<Project>__02_AUDIT_SECOND_WAVE.md
<Project>__03_AUDIT_PERFORMANCE.md
<Project>__00_AUDIT_ALL_3.md
_history\...
```

AUDAPACK audit storage owns canonical physical ALL_3 generation.

The Widget may keep a browser fallback copy/download, but must not create a second competing disk format.

---

## H13. GUI live integration

After successful wave/ALL publication:

- invalidate only the relevant audit snapshot where practical;
- refresh readiness;
- refresh temperature timestamp;
- recompute ALL hash;
- if ALL hash changed, clear old `COPIED` state and show `NEW`;
- do not aggressively rescan every project in a hot loop.

Expected visible example:

```text
MAIN0
FastPrompter
AUDIT HOT · 3/3 · ALL · NEW
[ COPY AUDIT ]
```

---

## H14. Component Center

GUI must expose the integrated state:

```text
BROWSER INTEGRATION

Widget: AUDAPACK Widget <version>
Status: CONNECTED / UPDATE / UNKNOWN
Registry: revision ...

Bridge: AUDAPACK Bridge <version>
Status: RUNNING / STOPPED / ERROR
Task: AUDAPACK Bridge
Port: 17843

[ Install / Update Widget ]
[ Start Bridge ]
[ Stop Bridge ]
[ Repair ]
[ Copy Token ]
[ Open Audit Root ]
[ Autostart ✓ ]
```

Legacy `ACBBridge` should be shown only as migration residue/problem, not as a normal component.

---

## H15. Tampermonkey install/update flow

The fresh bundled script is the maintained source of truth:

```text
resources\AUDAPACK_WIDGET.user.js
```

AUDAPACK GUI manages its install/update workflow as far as browser security permits.

Do not silently bypass Tampermonkey confirmation.

Widget must report/version-handshake with Bridge so GUI can identify stale script versions.

---

## H16. Browser queue performance

Registry awareness must not create a new polling disaster.

Requirements:

- cache registry snapshot;
- bounded refresh TTL/event-driven refresh;
- one queue enumeration per status snapshot maximum where possible;
- no full transcript reclassification merely to draw target group/slot;
- no giant GM writes for unchanged registry/UI state.

---

## Tests

Use production-equivalent threading/server behavior.

Required:

1. health + component identity;
2. authenticated logical project registry;
3. registry never exposes physical path;
4. exact project match;
5. alias match if implemented;
6. ambiguous project rejected/unresolved;
7. unknown project retained/routed to `_UNASSIGNED`;
8. MAIN0 routing;
9. MAIN1 routing;
10. SIDE0 routing;
11. SIDE1 routing;
12. project move affects next run destination;
13. stale Widget group cannot override Bridge routing;
14. immutable run project identity;
15. same-run project mismatch rejects;
16. concurrent same-run waves serialize;
17. concurrent different runs succeed;
18. duplicate receipt same content is idempotent;
19. conflicting duplicate rejects before overwrite;
20. unique history identities;
21. ALL only after valid 3/3;
22. unavailable audit root remains queued;
23. GUI invalidates copied state after new ALL;
24. registry cache does not hot-poll;
25. full browser -> bridge -> disk -> GUI flow.

---

## Acceptance gate

Wave H passes only when a fresh audit made by the maintained Tampermonkey Widget is durably delivered through AUDAPACK Bridge and appears automatically in the folder implied by the project's current MAIN/SIDE assignment, with canonical wave files/ALL_3 and correct GUI state.
