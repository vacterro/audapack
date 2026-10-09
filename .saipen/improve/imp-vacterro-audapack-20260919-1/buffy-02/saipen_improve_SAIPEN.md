agent: buffy-02
role: core
model_or_runtime: unknown
project: vacterro-audapack
saipen_version: 8.0.1
protocol_fingerprint: sha256:a3fdafed4f855150b16e9d3304381267e887fce246b7b8e0a98d7487792f3afe
source_head: 38a7cc48ef0ed635e6f7e6beadf13669ff2a5fe1
source_tree_fingerprint: git-delta-v1:1d8e12396597fd9c6843c4d589993f57833d33c05553329b8a319ecd28ad7df5
discovery_model: git-delta-v1
context_scope: SAIPEN audit, phase DONE
context_available: partial
report_status: complete

## RUN 1

Semantic SAIPEN audit (seat buffy-02, scope SAIPEN, phase DONE). Four observations from real work in this project on 19.09.26: two project gate-reliability defects, one protocol grammar trap reproduced twice, and one already-fixed stale-contract family recorded as baseline. Every finding carries one observable expected/actual/evidence triple.

IMP-001 [P2] [PROJECT_VIOLATION] [reproduced] [ticket]
expected: the repository-declared widget suite command (tests/widget/README.md: `node --test tests/widget/*.test.js`) runs every suite and prints its summary, so the declared gate can be reproduced by any seat; a suite file that leaves the process alive must fail bounded instead of hanging the gate
expected: a suite that keeps a live handle after its tests complete can never be accepted as the canonical command for a gate
actual: on Node v24.15.0 the directory run reproducibly stalls after tests/widget/w2-002-ownership.test.js completes (all 11 tests pass, no `# tests/# pass/# fail` summary, process never exits); `node tests/widget/w2-002-ownership.test.js` direct, `--test-isolation=none` and `--test-force-exit` all stall identically; the same command used during T-204 also stalled at w6-004-saihandoff-archive-send.test.js before that suite's stale send-contract tests were corrected; a bounded per-file runner completes every file
evidence: full-directory run redirected to a file: 11 w2-002 result lines, then no summary for >10 minutes (killed); per-file bounded runner (Start-Process + WaitForExit(120000)): `TOTAL pass=554 fail=0 hangs=1 [w2-002-ownership.test.js]`; same file with an explicit `--test-name-pattern` alternation: 11 pass, exit 0; Node v24.15.0; README.md line 29 still documents the directory glob form
recurrence: any project whose canonical suite command is the directory glob inherits a gate that cannot terminate on the current runtime; the failure is invisible to a CI that only reads the exit code because the process never returns one
weak_model: a weak model running `node --test tests/widget/*.test.js` sees output that looks green (all ✔) and no failure, then hangs; it cannot distinguish a passing suite from a dead gate. Fix priority: make the suite terminate (find and release the lingering handle in the harness/fake-timer teardown), then pin the exact command in README/CI, then prose

IMP-002 [P2] [PROJECT_VIOLATION] [reproduced] [ticket]
expected: `python -m pytest -q` exits 0 when the suite reports zero failures and skips, so the canonical gate's exit code is a trustworthy verdict
actual: two consecutive full runs reported `1897 passed, 5 skipped` (and an `--ignore` run `1874 passed, 5 skipped`) and then the interpreter terminated with exit code 0xC0000005 (STATUS_ACCESS_VIOLATION); the crash is at interpreter teardown and is independent of any single test file, so a wrapper reading only the exit code records a red gate for a green suite
evidence: `python -m pytest -q` -> summary `1897 passed 5 skipped 1 warning in 458.44s`, `$LASTEXITCODE=-1073741819`; same result `-1073741819`; `python -m pytest -q --ignore=tests/test_browser_bridge_widget_integration.py` -> `1874 passed 5 skipped`, same exit code; isolated subsets (integration test alone, ui_qt/test_state_columns.py alone) exit 0; the same teardown exit code is already recorded at E-1457 (T-200)
recurrence: any gate consumer that trusts the process exit status (CI, SAIPEN verify checkpoints, human operators) is misled; the crash predates T-204/T-205 and survives them
weak_model: a weak model sees a nonzero exit with `1897 passed` and cannot tell suite failure from interpreter teardown; it may report FAIL or, worse, fabricate PASS. Fix priority: bounded diagnosis/quarantine of the teardown crash (which module leaves the faulting state), then a wrapper or marker that keeps the verdict truthful until fixed

IMP-003 [P2] [PROTOCOL_VIOLATION] [observed] [ticket]
expected: a VERIFY PASS event may state its pre-fix red control honestly; when the verification-evidence grammar vetoes failure words, the refusal names the legal way to record that red half (`REGRESSION-EVIDENCE FAIL verifier:<hex> subject:<hex> -- <what ran>`), so the seat can comply without deleting evidence
actual: `saipen transition REVIEW T-204` refused twice with REFUSE [INCOMPLETE_TICKET] because the newest PASS event's detail named the pre-fix controls (`1 failed`, `3 failed`, `hung`); the refusal repeated the received text and asked for a PASS checkpoint but did not name the anchored REGRESSION-EVIDENCE grammar and did not say that a counted failure shape anywhere in the event text vetoes the README-style PASS token; the session only reached REVIEW after writing a third PASS event stripped of every red-control word, i.e. weaker evidence was required to proceed
evidence: two REFUSE [INCOMPLETE_TICKET] responses quoting the two PASS checkpoints ("VERIFY -> REVIEW requires explicit verification evidence for ticket T-204 (got: verify -> PASS ... 1 failed ...)" and the 74-pass/565-pass variant containing `fail`); the third PASS event without failure tokens transitioned to REVIEW; LOG E-1514..E-1517; grammar at `saipen_engine/log.py:805 _FAIL_COUNTED_RE` and `:813 _claims_failure`
recurrence: cross-project: phases/verify.md requires both halves of a red/green control while the ordinary PASS event channel vetoes the red half, so every seat that follows verify.md literally is pushed into either a second weaker PASS line or a fabricated REGRESSION-EVIDENCE anchor; the same refusal shape is reproducible for any ticket whose PASS detail mentions a nonzero failure count
weak_model: a weak model told to report `conf: high` with the pre-fix FAIL in the same line is refused, and the refusal's next command merely restates the checkpoint; a compliant weak model then strips the red evidence or invents an anchor. Fix priority: refusal/validator route first (the INCOMPLETE_TICKET response names the REGRESSION-EVIDENCE grammar and the machine-veto rule), canonical example in phases/verify.md second, prose last

IMP-004 [P3] [PROJECT_VIOLATION] [reproduced] [note]
expected: the Node widget suites assert the current product contract, where a Send click is never acceptance and a verified positive transition decides SENT
actual: five suites still encoded the old click-is-sent contract (w2-002 two executePreset tests, w3-002 #6, w4-006 #6, w6-003 #3 failing; w6-004-saihandoff hanging in the acceptance wait) while production already required positive acceptance evidence; T-204 corrected the fixtures test-side (no production widget byte changed, @version stays 0.0.61)
evidence: pre-fix bounded per-file run: `w3-002 pass=13 fail=1`, `w4-006 pass=12 fail=1`, `w6-003 pass=14 fail=1`, `HANG w2-002-ownership.test.js`, `HANG w6-004-saihandoff-archive-send.test.js`; post-fix: 565 pass 0 fail across 48 files (w6-004 43 pass, w2-002 11 pass); widget `@version 0.0.61` unchanged and release-identity suite green
note: already-fixed baseline, not a new ticket. Recorded because the same stale-contract class can silently coexist with a green full-suite claim when the declared suite command cannot terminate (IMP-001) and the gate exit code is not trustworthy (IMP-002).
