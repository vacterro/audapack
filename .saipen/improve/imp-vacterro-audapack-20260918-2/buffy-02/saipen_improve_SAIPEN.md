agent: buffy-02
role: core
model_or_runtime: unknown
project: vacterro-audapack
saipen_version: 8.0.1
protocol_fingerprint: sha256:0bab8e3dd3931bdd392234e626e24d9f7f3be56fb5cdece5e2ad1be54dcc7185
source_head: 38a7cc48ef0ed635e6f7e6beadf13669ff2a5fe1
source_tree_fingerprint: git-delta-v1:a9c918b7a35a4c5421e47b6f1a72b0c1470482057a23b64228bcda1452801015
discovery_model: git-delta-v1
context_scope: SAIPEN audit, phase DONE
context_available: partial
report_status: complete

## RUN 1

Semantic SAIPEN audit (seat buffy-01, scope SAIPEN). One protocol-violation root cause discovered while resuming this project's goal run, plus the canonical-gate defect it exposed. Every finding carries one observable expected/actual/evidence triple; reproduction is by read-only import of the engine.

IMP-001 [P1] [PROTOCOL_VIOLATION] [proven] [ticket]
expected: a saipen ingress payload is the operator's request text; a shell redirect or pipe operator after an ingress verb is classified as shell and never extracted as the request nor converted into a --hex transport route; the obligation records only bytes a refusal refused as a REQUEST, so BOOT's next route persists request text only
actual: guard_events.py:954-975 (ingress_payload) takes head[2] (everything after the ingress verb) as the request, rejecting only a leading '-' (line 973) and never consulting _SHELL_SYNTAX_CHARS (line 184): 'saipen start 2>&1' -> '2>&1'; 'saipen start > out.txt' -> '> out.txt'; 'saipen start | cat' -> '| cat'; 'saipen start 2>/dev/null' -> '2>/dev/null'. ingress_rewrite (978-994) makes each a 'saipen start --hex <hex>' route; map_event (1286-1295) routes there because '>'/'&' are in _SHELL_SYNTAX_CHARS so _saipen_cli_tokens bails; 1469-1497 arms INGRESS_TRANSPORT_UNSAFE and _record_pending_ingress (1413-1443) records the obligation; entry.py:232-245 then discharges it and mints ticket+receipt with witness=transport_obligation
evidence: read-only import of guard_events: ingress_payload('saipen start 2>&1')=='2>&1'; ingress_rewrite('saipen start 2>&1')=='saipen start --hex 323e2631'; ingress_payload('saipen start | cat')=='| cat'; ingress_payload('saipen start --json')==None; sha256(b'2>&1')==2642b5d63833864f9c0c6138c0a17d8c320ddd6258dfcdaf7a05abd9fbf24a8a == the recorded obligation digest. Live: 'saipen start 2>&1' on this project produced E-1450 SRC-055->T-199 (user_explicit) and E-1451 claimed; SRC-055.md body '2>&1'; BOARD ## DOING 'T-199 [P1] 2>&1'; STATE phase SCOUT/task T-199. Retire refuses (needs a separate operator-authority capsule; SRC-055 is punctuation). Report: .saipen/evidence/RAPORT-SAIPEN-INGRESS-SHELL-OPERATOR-20260918/REPORT.md; packet: %LOCALAPPDATA%/saipen/protocol_incidents/inbox/SAI-DEFECT-20260918-ingress-shell-operator-as-request.md
recurrence: any shell habit (2>&1, | cat, | more, > log, 2>/dev/null) after an ingress verb silently becomes protocol Work carrying OPERATOR AUTHORITY (transport_obligation witness), so no downstream gate can tell it from a real request. This reopens the class T-1372/T-1376 believed closed: the obligation faithfully persists a payload the parser never should have produced.
weak_model: a weak compliant model is TOLD by BOOT.md to run the refusal's --hex route verbatim; following that instruction commits the garbage. Fix priority: (1) parser rejection -- a payload whose first char is in _SHELL_SYNTAX_CHARS returns None (a request never begins with >, <, |, &, ;); (2) map_event classifies a shell-syntax tail as ordinary shell, not an ingress transport; (3) a payload with no alphanumeric char is a defect, not a request, and is never recorded as an obligation. Prose last; no new state field needed since the fix is a rejection.

IMP-002 [P2] [PROJECT_VIOLATION] [reproduced] [ticket]
expected: the canonical test command `python -m pytest -q` runs the suite; an operator-only live-acceptance script is excluded from collection rather than aborting the whole run with a collection error
actual: tests/test_target_g_live.py drives a real Chrome over CDP at IMPORT time (subprocess.Popen line 26, playwright connect_over_cdp lines 29-30) to exercise the T-198 Target-G ZIP acceptance by hand; it defines no test_ function and is untracked, so pytest imports it during collection, the conftest no-browser sentinel (tests/conftest.py:39) raises, and the whole run ends '1 error in 1.53s' before any test executes
evidence: `python -m pytest --collect-only -q` -> 'ERROR collecting tests/test_target_g_live.py ... RuntimeError: test tried to launch a real browser: C:\Program Files\Google\Chrome\Application\chrome.exe (in <session>) -- stub the launcher instead'; 'Interrupted: 1 error during collection'; 1790 collected, 0 run. `git ls-files --error-unmatch tests/test_target_g_live.py` -> pathspec did not match (untracked). After adding `collect_ignore = ["test_target_g_live.py"]` to tests/conftest.py: `python -m pytest -q` -> 1785 passed 5 skipped 0 failed
recurrence: broader than one file. Any untracked operator/scratch test_*.py under tests/ with module-level side effects (browser, network, filesystem) aborts the canonical gate, and the conftest browser ban -- correct in intent -- turns the side effect into a collection error with no route. The durable shape is a collection policy for operator scripts, not a per-file workaround.
weak_model: a weak model running the declared `python -m pytest -q` gate gets a single collection ERROR and cannot tell the suite is healthy; it may fabricate a pass or believe the tree is red. Fix priority: a declared collection policy (operator-script naming convention or an explicit collect_ignore owned by tests/conftest.py) first; validator fail-closed on an untracked side-effecting test_*.py second; canonical example third.

IMP-003 [P3] [OTHER] [observed] [note]
expected: the repository's declared lint gate `ruff check .` is green on the shipped tree
actual: it reported 11 findings across 5 files (audapack/packing.py I001; audapack/saipen_manifest_gate_test_support.py F401 x2; tests/test_saipen_manifest_gate.py F401; tests/repro_widget_geometry.py I001/F401 x2/W293; scripts/probe_chatgpt_live.py I001/W293 x3; tests/test_target_g_live.py E402 x2/I001); after mechanical fixes plus one per-file E402 ignore for the operator live script, `ruff check .` passes
evidence: `ruff check .` before -> 15 findings; after -> 'All checks passed!'; `python -m compileall -q audapack scripts` exit 0; `pytest tests/test_packing.py tests/test_fidelity.py` -> 171 passed; `pytest tests/test_saipen_manifest_gate.py` -> 6 passed
note: not a new ticket. The lint residue was pre-existing (recorded in E-1433's T-197 VERIFY) and was swept as part of the goal-driven convergence run T-200, not a distinct SAIPEN protocol defect. Recorded as the truthful baseline for this audit; no PROTOCOL_VIOLATION claimed.
