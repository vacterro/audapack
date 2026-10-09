agent: buffy-02
role: core
model_or_runtime: unknown
project: vacterro-audapack
saipen_version: 8.0.1
protocol_fingerprint: sha256:d1bbd086cd84b933a78c06328d10fd4f5ecde721c92ab9462dc465de3b6f8727
source_head: 38a7cc48ef0ed635e6f7e6beadf13669ff2a5fe1
source_tree_fingerprint: git-delta-v1:abdbbfe688434e72149a29d71f430557fd27589e519952c67e9aeb4a36e30062
discovery_model: git-delta-v1
context_scope: SAIPEN audit, phase DONE
context_available: partial
report_status: draft

## RUN 1

## RUN 1

Semantic SAIPEN audit (seat buffy-02, scope SAIPEN, phase DONE) on tree git-delta-v1:abdbbfe6884 at HEAD 38a7cc4. Three observations reproduced live in this session: one guard-classification defect that blocks the audit's own GATE-proof inputs, one declared-gate/declared-matrix contradiction in the working tree, and one carried-debt baseline recorded as a note. Every finding carries an observable expected/actual/evidence triple.

IMP-001 [P2] [PROTOCOL_VIOLATION] [reproduced] [ticket]
expected: in an idle DONE project the guard admits the bounded, read-only probes an audit seat needs to establish proof, and a documented read-only command is either admitted or refused with a diagnostic that names the command's own classification problem -- not an unrelated NO_ACTIVE_WORK that says a mutation requires active Work
expected: the improve AUDIT ASSIGNMENT advertises proof_levels [UNIT, COMPOSITION, CANONICAL, GATE, PROVENANCE]; GATE proof means running the repository gate, so the runtimes that gate executes (node, python) are audit inputs and must be reachable in the same state the assignment is issued
actual: in the idle DONE bound project the guard routes read-only probes to action=shell and refuses them NO_ACTIVE_WORK, while sibling read-only reporters in the identical state are admitted; `python --version`, `node --version`, `saipen permissions --json`, `saipen explain-next --json`, `Get-Command saipen`, and `(Get-ChildItem tests/widget -Filter *.test.js).Count` were all refused with `SAIPEN_GUARD_REFUSAL: NO_ACTIVE_WORK ... next: saipen start '<the task, one line>'`, so the seat cannot even pwd its own runtime versions or enumerate the suites the assignment calls GATE proof
evidence: admitted in the same session and same cwd: `git rev-parse HEAD` -> 38a7cc48ef0ed635e6f7e6beadf13669ff2a5fe1; `echo probe-ok` -> probe-ok; `saipen status --json`; `saipen validate --json`; `saipen improve status --json`; `saipen improve sweep-queue imp-vacterro-audapack-20260919-7 --json`; `Get-Content`, `Test-Path`, `git status --short`, `git diff --stat`. Refused: `node --version`, `python --version`, `saipen permissions --json`, `saipen explain-next --json`, `Get-Command saipen`, `(Get-ChildItem ...).Count`. The SAIPEN installation's own board already carries the same class at .saipen/BOARD.md:5 (T-1402) and :6 (T-1401), so the defect is cross-project and not local to AUDAPACK
recurrence: every audit seat asked for GATE proof in an idle project hits the same wall, because the proof vocabulary and the guard's admitted-verb set are owned by different layers and neither consults the other
weak_model: a weak model told its assignment requires GATE proof runs `node --version` to check the runtime, gets NO_ACTIVE_WORK with `next: saipen start '<the task>'`, and cannot tell that the refusal is about the guard's shell-verb classifier rather than about its own state. Fix priority: guard/validator route first (the refusal names the command's real classification), one owner for the read-only verb policy second, prose last

IMP-002 [P2] [PROJECT_VIOLATION] [observed] [ticket]
expected: the repository's declared widget gate and its declared CI matrix agree, and the exact command CI runs is a command the repository has proven terminates
expected: T-206's own verify clause offered two exits -- release the lingering handle that keeps the process alive, or pin the bounded per-file runner exactly in tests/widget/README.md and CI -- so a matrix that adds a runtime must pair with a gate proven to terminate on it
actual: the working tree adds Node 24 to the widget CI matrix while tests/widget/README.md still names `node --test tests/widget/*.test.js` as "the canonical full-suite gate" -- the directory-glob form T-206 recorded as stalling on Node v24.15.0 with no summary and no exit -- so the newly-added CI leg runs a gate that the repository already knows never returns, turning a local hang into a CI job that can only hit the job timeout
evidence: `git diff HEAD -- .github/workflows/ci.yml` -> `node-version: [18, 20, 22, 24]` (was `[18, 20, 22]`); ci.yml:58 runs `node --test tests/widget/*.test.js`; tests/widget/README.md:29 declares that same glob "the canonical full-suite gate"; T-206 (BOARD ## DONE) verify records the directory run stalling after w2-002 completes on Node v24 with the per-file runner completing every file; the README's added sentence (a run that prints results and then hangs is still a gate failure) states the hazard without removing it
recurrence: any project that widens a runtime matrix without binding the matrix entry to a gate command proven on that runtime repeats this; the failure is silent to CI status because a hung job is indistinguishable from a slow one until the timeout
weak_model: a weak model reading README sees "canonical full-suite gate" and runs it on Node 24 exactly as CI does, hangs, and -- with no summary printed -- cannot distinguish a green suite from a dead gate. Fix priority: pin the bounded per-file runner in README and CI, or release the lingering handle, then prose

IMP-003 [P3] [OTHER] [observed] [note]
expected: a bounded CLEAN pass that deliberately leaves a red strict-core gate records that decision where the next route can see it, so DONE does not advertise a maintain/continue route over an unaddressed gate failure
actual: saipen status --json reports conformance CURRENT_FAIL (canonical validator FAIL, exit_code 1, gate core) and saipen validate --json reports 118 problems / 92 warnings, while the same status block emits next_action/computed_next_action `saipen continue` and automation disposition CONTINUE reason maintain; the deliberate non-repair is stated only in LOG E-1649 prose, not in any machine surface the router consults
evidence: `saipen status --json` -> conformance "CURRENT_FAIL", conformance_status.receipt.exit_code 1, gate core, validator summary "Validation FAILED: 118 problem(s), 92 warning(s)"; the same payload -> next_action "saipen continue", computed_next_action "saipen continue", automation.disposition "CONTINUE", automation.reason_code "closure-conditions-C1-C2-C3-C4-C5-C6-C7"; LOG E-1649 is the only place the non-repair decision is stated
note: accepted carried debt, not a new ticket. T-211's own DEC (E-1649) says the 101 legacy closure-evidence FAILs need real re-verification and that scrubbing DONE text would silence rather than prove them. Recorded because the gap between the red gate and the continue route is invisible to a reader who looks only at next_action
