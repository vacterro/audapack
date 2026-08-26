# Board

## DOING

## TODO
- [ ] T-24 [HIGH] Wave N Qt production cutover / Tkinter removal / final UI parity | verify: Qt default launcher, full feature parity, release audit
- [ ] T-13 [LOW] Reconcile legacy raw-named canonical artifact paths with new fs-safe naming | verify: pytest regression proving fs-safe name resolution

## DONE
- [x] T-19 [HIGH] Wave M model-native DnD and targeted Qt model mutation architecture | verify: model_reset_count == 0 on move/swap; Qt ItemIsDragEnabled/ItemIsDropEnabled; optimistic drop with rollback | owner: opencode | claim_time: 2026-08-26T18:05:00Z
- [x] T-20 [HIGH] Wave M background task runner: async packing, bridge status/lifecycle, startup enrichment | verify: UI never blocks during ZIP packing or Bridge requests; task coalescing | owner: opencode | claim_time: 2026-08-26T18:05:00Z
- [x] T-21 [HIGH] Wave M targeted audit refresh & memory-based temperature calculation | verify: single-project audit events trigger single-row dataChanged; zero disk reads on temperature ticks | owner: opencode | claim_time: 2026-08-26T18:05:00Z
- [x] T-22 [HIGH] Wave M canonical audit path index optimization & lightweight snapshot cache | verify: canonical root/group/project checked first; signature cache; repeated file reads removed | owner: opencode | claim_time: 2026-08-26T18:05:00Z
- [x] T-23 [HIGH] Wave M performance instrumentation, scale benchmarks (24/60/120/300) & stress tests | verify: 100 moves, 100 audit events, synthetic scale matrix passing with 0 unintended model resets | owner: opencode | claim_time: 2026-08-26T18:05:00Z

Historical baseline notes (pre-SAIPEN accomplishments, no protocol-tracked closure):
- Full mature AICHATBUTTONS-derived Widget restored as `AUDAPACK_WIDGET.user.js` rather than the earlier minimal replacement.
- AUDAPACK Bridge service identity and API v2 health payload exist server-side.
- Backend registry endpoints and SIDE1+ auto-registration exist.
- Windows Scheduled Task manager and legacy takeover modules exist.
- `%LOCALAPPDATA%\AUDAPACK` runtime/config/state/secrets structure introduced.
- Unified per-run history directory introduced.
- GUI cross-process audit generation polling introduced.

## BLOCKED
- [ ] T-012 (WJ-012) Windows end-to-end acceptance: existing project, new SIDE1+ project, move, offline queue, Auto3 3/3 -> ALL_3 -> GUI NEW/COPIED, autostart, legacy absence, secret scan. | owner: opencode | claim_time: 2026-08-26T03:07:47Z | blocker: Real Windows end-to-end production acceptance requires live operator interaction this headless agent session cannot perform: Tkinter GUI project moves (Scenario D/E), Scheduled Task manual trigger + stop/start cycles (A), real Tampermonkey widget flows incl. resolve/queue/offline (B/C/E), full Auto3 chain in a real chat (F), GUI NEW/COPIED clipboard flow (G), legacy-runtime-absence cutover proof (I). Already-proven equivalents from this wave: path containment + traversal delivery (T-001), secret-free serialization + zip content scan red/green (T-002), transactional takeover gate ordering with stubbed schtasks (T-003), registry concurrency + save-failure honesty (T-004), strict wave validation (T-005), hashed run identity + conflict 409s (T-006), generation lock (T-007), API v2 handshake contract server-side (T-008), root pytest 114 PASS green. Unblock path: operator executes spec section 14 Scenarios A-I on the production machine with installed Widget + Bridge task, then a fresh session closes this ticket with evidence.
- [ ] T-009 (WJ-009) Real Tampermonkey migration: preserve durable preferences/token through a verified migration path; do not fake cross-userscript GM storage access. | owner: opencode | claim_time: 2026-08-26T02:48:24Z | blocker: Requires live Tampermonkey cross-userscript storage verification in a real browser -- not executable from this headless agent session (browser automation prohibited here; spec sections 11/24 forbid claiming migration success from unverified GM_getValue behavior or fabricating browser results). Clean tree preserved: zero code changes made. Unblock path: operator runs a manual export/import probe in Tampermonkey (old AICHATBUTTONS vs AUDAPACK_WIDGET identity sandbox) and reports GM storage visibility, then a fresh session implements the smallest verified strategy.
