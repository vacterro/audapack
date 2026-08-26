// ==UserScript==
// @name         AUDAPACK Widget
// @namespace    https://github.com/vacterro/audapack
// @version      0.0.01
// @description  Universal AI prompt buttons & Auto3 audit engine — AUDAPACK Widget
// @author       AUDAPACK
// @match        https://chat.openai.com/*
// @match        https://chatgpt.com/*
// @match        https://claude.ai/*
// @match        https://chat.deepseek.com/*
// @match        https://chat.qwen.ai/*
// @match        https://qwen.ai/*
// @match        https://tongyi.aliyun.com/*
// @match        https://grok.com/*
// @match        https://x.com/i/grok*
// @match        https://gemini.google.com/*
// @match        https://gemini.google.com/app*
// @match        https://copilot.microsoft.com/*
// @match        https://www.bing.com/chat*
// @match        https://kimi.moonshot.cn/*
// @match        https://kimi.com/*
// @match        https://duck.ai/*
// @match        https://duckduckgo.com/*
// @match        https://chat.mistral.ai/*
// @match        https://huggingface.co/chat/*
// @match        https://www.perplexity.ai/*
// @match        https://poe.com/*
// @match        https://pi.ai/*
// @match        https://www.phind.com/*
// @match        https://you.com/*
// @grant        GM_setValue
// @grant        GM_getValue
// @grant        GM_deleteValue
// @grant        GM_addStyle
// @grant        GM_setClipboard
// @grant        GM_xmlhttpRequest
// @grant        GM_listValues
// @grant        GM_addValueChangeListener
// @connect      127.0.0.1
// @connect      localhost
// @run-at       document-start
// ==/UserScript==

(function () {
  'use strict';

  const STORAGE_KEY = 'ai_chatbuttons_v6';
  const STATE_VERSION = 35;
  const BUILTIN_REVISION = 8;
  const MAX_CATEGORIES = 10;
  const MAX_PRESETS = 20;
  const PANEL_WIDTH = 400;
  const PANEL_HEIGHT = 510;
  const SUPER_COMPACT_WIDTH = 300;
  const SUPER_COMPACT_HEIGHT = 28;
  const PANEL_SIZES = Object.freeze({
    compact: Object.freeze({ width: 340, height: 420, label: 'Small' }),
    normal: Object.freeze({ width: PANEL_WIDTH, height: PANEL_HEIGHT, label: 'Normal' }),
    large: Object.freeze({ width: 480, height: 620, label: 'Large' })
  });
  const OPACITY_LEVELS = Object.freeze([100, 75, 50, 25]);
  const PANEL_EDGE_MARGIN = 8;
  const AUTO_LEGACY_RUNTIME_KEY = 'ai_chatbuttons_auto_audit_runtime_v2';
  const AUTO_RUNTIME_PREFIX = 'ai_chatbuttons_auto_audit_runtime_v4:';
  const AUTO_LEASE_PREFIX = 'ai_chatbuttons_auto_audit_lease_v1:';
  const AUTO_LEGACY_SESSION_KEY = 'ai_chatbuttons_auto_audit_v1';
  const AUTO_TAB_SESSION_KEY = 'ai_chatbuttons_auto_tab_id_v1';
  const AUTO_DRAFT_SESSION_KEY = 'ai_chatbuttons_auto_draft_id_v1';
  const AUTO_START_HANDOFF_SESSION_KEY = 'ai_chatbuttons_auto_start_handoff_v1';
  const AUTO_START_PREPARE_TTL_MS = 600000;
  const AUTO_START_SENT_TTL_MS = 300000;
  const AUTO_START_ROUTE_COMMIT_WINDOW_MS = 20000;
  const AUTO_START_HARD_NAV_BOOTSTRAP_MS = 120000;
  const AUTO_AUTH_HOLD_RETRY_MS = 5000;
  const AUTO_LAST_STABLE_CHAT_SESSION_KEY = 'ai_chatbuttons_last_stable_chat_v1';
  const AUTO_A3_INTENT_SESSION_KEY = 'ai_chatbuttons_a3_intent_v1';
  const AUTO_ROUTE_TRANSIENT_GRACE_MS = 30000;
  const LOCAL_TITLE_REAPPLY_MIN_MS = 800;
  const WIDGET_BOOTSTRAP_RETRY_DELAYS_MS = Object.freeze([0, 10, 25, 50, 100, 250, 500, 1000, 2000, 5000]);
  const AUTO_START_RECOVERY_DELAYS_MS = Object.freeze([
    120, 350, 800, 1600, 3000, 5000, 8000, 12000, 20000, 30000, 45000, 60000, 90000, 120000
  ]);
  const AUTO_LEASE_TTL_MS = 30000;
  const AUTO_LEASE_RENEW_MS = 10000;
  const AUTO_LEASE_VERIFY_MS = 90;
  const AUTO_MAX_PARTIAL_CONTINUATIONS = 8;
  const AUTO_MAX_CONTINUE_GENERATING = 8;
  const AUTO_MAX_RETRIES = 3;
  const AUTO_MAX_STALL_NUDGES = 6;
  const AUTO_MAX_SIDECAR_RECOVERIES = 8;
  const AUTO_SIDECAR_RECOVERY_GRACE_MS = 6000;
  const AUTO_IDLE_STALL_GRACE_MS = 20000;
  const AUTO_LIVENESS_CHECK_MS = 2500;
  const AUTO_EXECUTION_GAP_RESET_MS = 15000;
  const AUTO_STAGE_TIMEOUTS = Object.freeze([60, 120, 180, 360]);
  const AUTO_DELAYS_MS = Object.freeze([500, 1200, 2500, 5000, 10000]);
  const AUTO_RESPONSE_STABLE_MS = 1200;
  const AUTO_OBSERVER_DEBOUNCE_MS = 650;
  const AUTO_SEND_REGISTER_TIMEOUT_MS = 12000;
  const AUTO_SEND_REGISTER_RETRY_MS = 15000;
  const AUTO_SEND_REGISTER_HARD_TIMEOUT_MS = 120000;
  const AUTO_MAX_SEND_REGISTRATION_RETRIES = 3;
  const AUTO_COMPOSER_HOLD_FAST_DELAYS_MS = Object.freeze([250, 600, 1200, 2200, 3500, 5000, 5000, 5000]);
  const AUTO_COMPOSER_HOLD_SAFETY_MS = 15000;
  const AUTO_SEND_RECEIPT_PREFIX = 'ACB_CHAIN_RECEIPT';
  const AUTO_COMMITTED_SEND_SESSION_KEY = 'ai_chatbuttons_committed_send_v1';
  const AUTO_COMMITTED_SEND_TTL_MS = 90000;
  const AUTO_AUDIT_RESULT_PREFIX = 'ai_chatbuttons_audit_result_v1:';
  const AUTO_AUDIT_RESULT_SIGNAL_KEY = 'ai_chatbuttons_audit_result_signal_v1';
  const AUDIT_FS_DB_NAME = 'ai_chatbuttons_files_v1';
  const AUDIT_FS_DB_VERSION = 1;
  const AUDIT_FS_STORE = 'handles';
  const AUDIT_FS_HANDLE_KEY = 'audit-output-directory';
  const AUDIT_OUTPUT_HINT = 'V:\\___VAC\\__K\\__CODE\\_AI_STUFF_AGENTIC\\__TO_AUDIT\\AUDITING_IMPLEMENTATION\\';

  const BRIDGE_DEFAULT_URL = 'http://127.0.0.1:17843';
  const BRIDGE_TOKEN_KEY = 'ai_chatbuttons_bridge_token_v1';
  const BRIDGE_JOB_PREFIX = 'ai_chatbuttons_bridge_job_v1:';
  const BRIDGE_QUEUE_SIGNAL_KEY = 'ai_chatbuttons_bridge_queue_signal_v1';
  const BRIDGE_FLUSH_LEASE_KEY = 'ai_chatbuttons_bridge_flush_lease_v1';
  const BRIDGE_FLUSH_LEASE_MS = 30000;
  const BRIDGE_REQUEST_TIMEOUT_MS = 12000;
  const BRIDGE_RETRY_DELAYS_MS = Object.freeze([2000, 5000, 15000, 30000, 60000, 120000, 300000]);
  const BRIDGE_API_VERSION = 2;

  const CHAT_RENAME_MAX_ATTEMPTS = 7;
  const CHAT_TITLE_GUARD_DELAYS_MS = Object.freeze([350, 1200, 4000, 15000, 60000, 300000]);
  const CHAT_TITLE_GUARD_TTL_MS = 960000;
  const CHAT_TITLE_VERIFY_DELAYS_MS = Object.freeze([160, 420, 900]);
  const CHAT_RENAME_RATE_STATE_KEY = 'ai_chatbuttons_rename_rate_v2';
  const CHAT_RENAME_GLOBAL_MIN_GAP_MS = 8000;
  const CHAT_RENAME_CONVERSATION_MIN_GAP_MS = 12000;
  const CHAT_RENAME_429_COOLDOWN_MS = 900000;
  const CHATGPT_LONG_PROMPT_THRESHOLD = 6000;
  const CHATGPT_ATTACHMENT_TIMEOUT_MS = 30000;
  const CHATGPT_PROMPT_DELIVERY_MODES = Object.freeze(['auto', 'file', 'text']);
  const AUDIT_ATTACHMENT_FILES = Object.freeze({
    core: 'AUDIT_CORE.md',
    second: 'AUDIT_SECOND_WAVE.md',
    performance: 'AUDIT_PERFORMANCE.md'
  });

  const AUDIT_CORE = "AUDIT CORE — deep read-only software correctness audit for implementation handoff.\n\nROLE\n\nYou are the AUDITOR, not the implementation agent.\n\nInspect the supplied project deeply, identify verified flaws, and return a high-value repair handoff for a separate implementation agent. Do not modify the audited implementation or project metadata during this pass.\n\nTARGET\n\nResolve the most recent explicit implementation target in the conversation: repository, archive, project tree, attached file, or pasted code.\n\nPrefer the newest explicit target when several exist.\n\nRepository: inspect the current supplied/default revision unless the user explicitly names another branch/commit.\nArchive: unpack fully and identify the real project root.\nSingle file: inspect the complete supplied file and directly relevant local contracts/dependencies when available.\n\nIf the target itself cannot actually be read, return BLOCKED with the exact missing artifact/access. Do not invent evidence.\n\nPROJECT ORIENTATION — FAST, READ-ONLY, NON-BLOCKING\n\nBefore the deep code audit, spend a small bounded setup pass understanding the project itself.\n\nAt the resolved project root, inspect these only when they exist:\n\n- `.saipen/`: root project STATE/BOARD/LOG plus only relevant KNOWLEDGE/kitchen material. Use it as optional historical/work-state context: prior fixes, known failures, active tasks, stale assumptions, baseline drift.\n- Git: use ordinary Git metadata/commands when available (HEAD, branch, status, useful recent history/diff). Do NOT crawl `.git/objects` or spend audit budget reading Git internals.\n- project manifests and lockfiles: package.json, Cargo.toml, pyproject.toml, requirements, go.mod, solution/project files, build config, etc.;\n- tests/fixtures, migrations, schemas, config, docs/contracts, CI/workflow files, scripts and entry points when relevant;\n- other project-local helper/state directories only when they materially explain runtime behavior.\n\nThese sources are ORIENTATION, not gates.\n\nRules:\n- `.saipen/`, Git, docs, tests, manifests, CI or helper folders may be absent. Continue normally.\n- Do not require an external SAIPEN installation, `saipen_home`, BOOT/CORE/MARKHUNT files, validator, portable protocol bundle, or any other out-of-project authority.\n- Do not initialize, repair, rebind, validate or modify `.saipen/`.\n- If `.saipen/` is stale, contradictory, incomplete, or references inaccessible external paths, record that briefly and continue.\n- Live implementation files are authoritative for current behavior.\n- Do not let project-management metadata consume the audit budget. The implementation is the primary target.\n\nBASELINE\n\nRecord the strongest truthful identity available:\n- Git: branch + current commit when readable;\n- archive: archive filename + hash/fingerprint when available;\n- file: filename + hash/fingerprint when available;\n- otherwise the clearest stable identity available.\n\nAUDIT METHOD\n\nBuild a compact map first:\n\nentry points -> parsing/validation -> state owners -> transitions -> core logic -> persistence/I/O -> recovery/error paths -> UI/output -> tests/contracts.\n\nThen follow real execution/data/state paths end-to-end. Audit correctness before style.\n\nHUNT VERIFIED ROOT DEFECTS\n\nPrioritize:\n- broken invariants and contradictory logic;\n- wrong defaults, branches, ordering or state transitions;\n- missing/incorrect validation and invalid partial-state handling;\n- duplicate/competing sources of truth;\n- duplicated implementations whose behavior can drift;\n- dead/unreachable/stale compatibility paths that still affect runtime;\n- partial migrations and config/schema/version drift;\n- persistence, serialization, import/export, restart, recovery and data-loss faults;\n- init/shutdown/teardown/ownership mistakes;\n- concurrency, repeated invocation, stale async result, retry, cancellation and idempotence faults where applicable;\n- UI state disagreeing with runtime state;\n- stale/replaced targets and incorrect fallback selection;\n- API/CLI/docs/config/tests contradicting actual execution;\n- tests that protect the wrong invariant or fail to cover a demonstrated failure.\n\nQUALITY BAR\n\n- Evidence before finding.\n- Root cause before symptom.\n- Merge symptoms sharing one root cause.\n- Preserve correct behavior.\n- No generic cleanup.\n- No speculative redesign.\n- No framework/dependency/telemetry/dashboard proposals unless required by a verified defect.\n- Never fabricate commands, output, test results, timings, paths, commits or reproduction evidence.\n- Use PARTIAL only when the wave genuinely cannot finish within the current execution/context budget. PARTIAL is a resumable machine checkpoint, not a request for user intervention; finish the current evidence cleanly and expect an automatic same-wave continuation.\n- Do not interact with external accounts, services, hosts, endpoints or infrastructure. Local/static project inspection and ordinary project tests are allowed when available and relevant.\n\nTEST EXECUTION / ENVIRONMENT LIMITS\n\nTreat test execution as evidence, not as an artificial gate that can invalidate an otherwise useful audit.\n\nClassify verification truthfully:\n- TEST_PASSED = the relevant intended tests/checks actually ran and passed.\n- TEST_FAILED = a relevant test/check actually ran and failed because of project behavior.\n- TEST_PARTIAL = only part of the relevant intended verification could run.\n- TEST_NOT_RUN_ENVIRONMENT = the intended relevant tests could not start/run because this execution environment lacks an external prerequisite.\n- TEST_NOT_APPLICABLE = there is no meaningful runnable test/check for this audited surface.\n\nEnvironment limitations include unavailable third-party packages, GUI/runtime/display facilities, system libraries, platform capabilities, network/package-install access, toolchains, services, or other prerequisites outside the supplied project.\n\nRules:\n- A dependency that the project correctly declares but this sandbox does not provide is an ENVIRONMENT LIMITATION, not a project defect.\n- If the implementation requires a dependency but the project itself fails to declare/package/document it where its normal runtime/build contract requires that declaration, that may be a real project defect when verified.\n- Do not fabricate a red/green test result for a command that never actually ran.\n- Do not label the whole audit BLOCKED merely because the complete test suite cannot execute. Continue with every meaningful verification still available.\n- Run unaffected tests/checks that do not require the missing prerequisite when practical.\n- Use static inspection, compile/type/syntax checks, focused pure-logic tests, fixture analysis, or other local verification as substitutes where they materially increase confidence.\n- Record the exact unavailable prerequisite and the exact command/error that prevented execution when known.\n- Reduce confidence only for findings whose verification materially depends on the unavailable capability.\n- Do not weaken, skip, rewrite, or make legitimate project tests optional merely to accommodate this sandbox.\n- Do not repeatedly burn audit budget trying the same unavailable installation/setup path. At most one bounded setup attempt is appropriate when the environment clearly supports it and the project declares the setup path.\n- Missing environment capability must never become a fake project ticket merely to make the audit look complete.\n- PARTIAL audit status is for an actually unfinished AUDIT WAVE due to execution/context budget, not merely for partial TEST COVERAGE. A wave may be STATUS ... COMPLETE while TEST_STATUS is TEST_PARTIAL or TEST_NOT_RUN_ENVIRONMENT if the code audit itself is complete and the limitation is explicitly recorded.\n\nEFFICIENCY\n\nDo not read the project alphabetically and do not repeatedly rescan stable areas.\n\nSpend most analysis on state ownership, transitions, persistence, lifecycle, routing, recovery, user-visible behavior and code implicated by real execution paths.\n\nSkip generated/vendor/build/cache output unless runtime or evidence makes it relevant.\n\nPRIORITY\n\nP0 = data corruption/loss, severe local safety/security defect, crash/unusable primary flow, or fundamental correctness failure.\nP1 = significant functional defect, lifecycle/recovery/integration failure, or high-probability user-visible breakage.\nP2 = lower-impact but real defect, concrete maintainability drift likely to cause failure, or missing regression coverage for a verified issue.\n\nFINAL HANDOFF\n\nReturn ONE code block only. It must be directly usable by a separate implementation agent without needing the audit conversation.\n\nHeader exactly:\n\nPROJECT_NAME: <name>\nDATE_TIME: <current session/local date-time when available; otherwise UTC>\nWAVE: AUDIT CORE\nTARGET: <what artifact/project was actually inspected>\nBASELINE: <identity>\nGIT_CONTEXT: <PRESENT branch@commit | ABSENT | UNREADABLE> - <brief note>\nSAIPEN_CONTEXT: <PRESENT | ABSENT | STALE | UNREADABLE> - <brief useful note>\nAUDIT_SCOPE: <compact modules/areas actually inspected>\nTEST_STATUS: <TEST_PASSED | TEST_FAILED | TEST_PARTIAL | TEST_NOT_RUN_ENVIRONMENT | TEST_NOT_APPLICABLE>\nTEST_LIMITATION: <NONE | exact unavailable prerequisite + command/error>\nVERIFIED_INSTEAD: <NONE | concise alternative verification actually performed>\nSTATUS: AUDIT_CORE: <COMPLETE | PARTIAL | BLOCKED>\nTICKETS: <count>\nHANDOFF: IMPLEMENTATION_AGENT\n\nThen tickets in priority order. Each ticket must use:\n\n[P0|P1|P2] [CORE-001] <path/module/symbol>\nEVIDENCE: <specific code/path/behavior proving the issue>\nDEFECT: <root cause and concrete consequence>\nREPAIR: <smallest correct implementation change; name exact areas when established>\nVERIFY: <specific regression test/check that proves the repair without breaking correct behavior>\n\nUse CORE-001, CORE-002... only within this handoff. One root cause per ticket.\n\nIf no verified defects exist:\nTICKETS: 0\nNO VERIFIED CORE DEFECTS.\n\nEnd:\nCORE_DONE_WHEN: <compact explicit implementation + verification gate>\n\nNo prose outside the code block.";

  const AUDIT_SECOND_WAVE = "AUDIT SECOND WAVE — complementary read-only audit for implementation handoff.\n\nROLE\n\nYou are the AUDITOR, not the implementation agent. Do not modify the audited implementation or project metadata.\n\nThis is a second independent lens over the SAME project after Audit Core. Its value is finding verified defects Core did not expose, not repeating Core with different wording.\n\nPRECONDITION / TARGET\n\nUse the same target lineage as the latest completed Audit Core in this conversation unless the user explicitly supplied a newer revision of that project.\n\nA matching completed Core result in the conversation is sufficient continuity. `.saipen/` is optional context only.\n\nIf no matching Core exists, return a concise BLOCKED handoff stating that Audit Core for this target is missing.\n\nPROJECT ORIENTATION — REUSE, THEN REFRESH\n\nReuse Core's established map when still valid.\n\nAt project root, quickly refresh only useful local orientation when present:\n- `.saipen/` root STATE/BOARD/LOG and relevant project memory;\n- Git current HEAD/branch/status/diff metadata through ordinary Git operations, never `.git/objects` crawling;\n- manifests/config/schema/migrations/tests/docs/CI/scripts relevant to changed or boundary behavior.\n\nNone of these are required. Do not require external SAIPEN protocol files or maintain `.saipen/`.\n\nIf current baseline differs from Core:\n- identify changed paths first;\n- revalidate only Core conclusions affected by those changes;\n- preserve unaffected evidence;\n- record the new baseline.\n\nSECOND-WAVE PURPOSE\n\nDo NOT perform Core again.\n\nDo not repeat a Core ticket unless it regressed, remains broken and is necessary to explain a new issue, or new evidence materially changes the diagnosed root cause/repair.\n\nAttack boundaries and failure behavior a first correctness pass commonly misses.\n\nLIFECYCLE / OWNERSHIP\n- cold and repeated startup;\n- partial initialization;\n- shutdown/teardown/cleanup;\n- restart/reopen after failure;\n- multiple writers to mutable state;\n- stale references/caches/subscriptions;\n- ownership changing across async steps.\n\nINPUT / BOUNDARIES\n- empty, missing, malformed, partial and maximum inputs;\n- unusual but valid Unicode, spaces, long paths, locale/platform differences when relevant;\n- optional-value combinations;\n- caller/callee disagreement over null/empty/error semantics.\n\nORDERING / REPEATABILITY\n- duplicate invocation;\n- double click/submit/dispatch;\n- out-of-order or delayed events;\n- cancellation/retry after partial work;\n- stale async completion overwriting newer state;\n- idempotence of migration/import/recovery/cleanup;\n- repeated action after restart.\n\nPERSISTENCE / RECOVERY\n- partial writes;\n- stale stored state after upgrade;\n- crash between related writes;\n- migration run twice;\n- serializer/parser asymmetry;\n- import/export round trip;\n- restart immediately after failure;\n- fallback selecting stale persisted state.\n\nERROR / CONTRACT DRIFT\n- swallowed errors and false success;\n- fallback masking the root cause;\n- API/CLI/config/schema/version/docs mismatch;\n- mocks/fixtures hiding integration behavior.\n\nUI / RUNTIME\n- visible state contradicting runtime;\n- impossible action enabled;\n- stale disabled reason;\n- stale/replaced DOM/state target;\n- reload/reopen/resize/maximize/restore/focus/blur leaving invalid state or unreachable controls.\n\nDUPLICATED TRUTH\n\nSpecifically hunt duplicated constants, selectors, validators, parsers, serializers, mappings, transitions, fallback precedence, path resolution and business rules. Consolidate only when duplication creates concrete drift/failure risk.\n\nQUALITY / EFFICIENCY\n\n- New verified root causes only.\n- Evidence before finding.\n- Root cause before symptom.\n- No checklist padding.\n- No speculative redesign.\n- No external-system interaction.\n- Never fabricate evidence or timings.\n- Use PARTIAL only for a genuine execution/context limit. PARTIAL is a resumable machine checkpoint and must not ask the user to supervise; an automatic same-wave continuation may follow.\n- Reuse Core facts instead of rereading unchanged internals.\n- Spend most budget at cross-module seams, lifecycle boundaries, persistence/recovery, and state transitions.\n\nTEST EXECUTION / ENVIRONMENT LIMITS\n\nTreat test execution as evidence, not as an artificial gate that can invalidate an otherwise useful audit.\n\nClassify verification truthfully:\n- TEST_PASSED = the relevant intended tests/checks actually ran and passed.\n- TEST_FAILED = a relevant test/check actually ran and failed because of project behavior.\n- TEST_PARTIAL = only part of the relevant intended verification could run.\n- TEST_NOT_RUN_ENVIRONMENT = the intended relevant tests could not start/run because this execution environment lacks an external prerequisite.\n- TEST_NOT_APPLICABLE = there is no meaningful runnable test/check for this audited surface.\n\nEnvironment limitations include unavailable third-party packages, GUI/runtime/display facilities, system libraries, platform capabilities, network/package-install access, toolchains, services, or other prerequisites outside the supplied project.\n\nRules:\n- A dependency that the project correctly declares but this sandbox does not provide is an ENVIRONMENT LIMITATION, not a project defect.\n- If the implementation requires a dependency but the project itself fails to declare/package/document it where its normal runtime/build contract requires that declaration, that may be a real project defect when verified.\n- Do not fabricate a red/green test result for a command that never actually ran.\n- Do not label the whole audit BLOCKED merely because the complete test suite cannot execute. Continue with every meaningful verification still available.\n- Run unaffected tests/checks that do not require the missing prerequisite when practical.\n- Use static inspection, compile/type/syntax checks, focused pure-logic tests, fixture analysis, or other local verification as substitutes where they materially increase confidence.\n- Record the exact unavailable prerequisite and the exact command/error that prevented execution when known.\n- Reduce confidence only for findings whose verification materially depends on the unavailable capability.\n- Do not weaken, skip, rewrite, or make legitimate project tests optional merely to accommodate this sandbox.\n- Do not repeatedly burn audit budget trying the same unavailable installation/setup path. At most one bounded setup attempt is appropriate when the environment clearly supports it and the project declares the setup path.\n- Missing environment capability must never become a fake project ticket merely to make the audit look complete.\n- PARTIAL audit status is for an actually unfinished AUDIT WAVE due to execution/context budget, not merely for partial TEST COVERAGE. A wave may be STATUS ... COMPLETE while TEST_STATUS is TEST_PARTIAL or TEST_NOT_RUN_ENVIRONMENT if the code audit itself is complete and the limitation is explicitly recorded.\n\nFINAL HANDOFF\n\nReturn ONE code block only, standalone enough for a separate implementation agent.\n\nHeader exactly:\n\nPROJECT_NAME: <name>\nDATE_TIME: <current session/local date-time when available; otherwise UTC>\nWAVE: AUDIT SECOND WAVE\nTARGET: <artifact/project inspected>\nBASELINE: <current identity>\nCORE_BASELINE: <identity from Core>\nGIT_CONTEXT: <PRESENT branch@commit | ABSENT | UNREADABLE> - <brief note>\nSAIPEN_CONTEXT: <PRESENT | ABSENT | STALE | UNREADABLE> - <brief useful note>\nAUDIT_SCOPE: <compact new/boundary areas inspected>\nTEST_STATUS: <TEST_PASSED | TEST_FAILED | TEST_PARTIAL | TEST_NOT_RUN_ENVIRONMENT | TEST_NOT_APPLICABLE>\nTEST_LIMITATION: <NONE | exact unavailable prerequisite + command/error>\nVERIFIED_INSTEAD: <NONE | concise alternative verification actually performed>\nSTATUS: SECOND_WAVE: <COMPLETE | PARTIAL | BLOCKED>\nTICKETS: <count>\nHANDOFF: IMPLEMENTATION_AGENT\n\nThen only NEW, REGRESSED, or materially RE-DIAGNOSED findings:\n\n[P0|P1|P2] [W2-001] <path/module/symbol>\nEVIDENCE: <specific evidence>\nDEFECT: <root cause and concrete consequence>\nREPAIR: <smallest correct implementation change>\nVERIFY: <specific regression verification>\n\nUse W2-001, W2-002... only within this handoff.\n\nDo not include unchanged Core tickets merely for completeness.\n\nIf no new defects exist:\nTICKETS: 0\nNO NEW VERIFIED SECOND-WAVE DEFECTS.\n\nEnd:\nSECOND_WAVE_DONE_WHEN: <compact implementation + regression gate>\n\nNo prose outside the code block.";

  const AUDIT_PERFORMANCE = "AUDIT PERFORMANCE / STABILITY / EFFECTIVENESS — third read-only audit wave for implementation handoff.\n\nROLE\n\nYou are the AUDITOR, not the implementation agent. Do not modify the audited implementation or project metadata.\n\nThis is the third lens over the SAME target after Audit Core and Audit Second Wave. Spend the audit budget on material responsiveness, latency, stability, bounded resource use and simpler effective execution without changing correct observable behavior.\n\nPRECONDITION / TARGET\n\nUse the same target lineage as the latest matching Core and Second Wave results in this conversation unless the user explicitly supplied a newer revision.\n\nBoth earlier waves must exist for this target. If either is missing, return a concise BLOCKED handoff naming the missing wave.\n\nPROJECT ORIENTATION — REUSE, THEN REFRESH\n\nReuse structural understanding from the first two waves.\n\nWhen present, quickly inspect only useful local context:\n- `.saipen/` root STATE/BOARD/LOG and relevant memory for recent implementation work;\n- Git current HEAD/branch/status/diff through ordinary Git commands, not `.git/objects` crawling;\n- performance-related config/build manifests/tests/benchmarks and only other project metadata needed to understand a hot path.\n\nThese are optional. Never require an external SAIPEN installation or modify `.saipen/`.\n\nIf baseline changed since Second Wave:\n- identify changed paths first;\n- revalidate only affected correctness/stability assumptions;\n- never optimize stale code paths.\n\nPRIMARY OBJECTIVE\n\nCorrectness and observable behavior are invariants. Reject benchmark cosmetics that damage determinism, accessibility, recovery, explicit state or useful error reporting.\n\nTrace frequent paths such as startup, button click, pointer move, keystroke/input, render/update, state transition, parsing, validation, serialization, local persistence/I/O, retry, recovery and shutdown.\n\nCOMPUTATION\n- repeated parsing/serialization/validation/normalization of unchanged data;\n- duplicate transforms/copies/allocations;\n- unnecessary full scans/sorts/filters;\n- repeated linear lookup inside loops and realistic O(n²) paths;\n- expensive fallback used on normal success.\n\nUI / DOM\n- broad/repeated document/tree scans;\n- selectors not scoped to a stable root;\n- repeated layout reads/writes and reflow thrash;\n- unnecessary rerender/rebuild/repaint;\n- synchronous work delaying visible input/button feedback;\n- listener multiplication and stale subscriptions;\n- detached-node retention;\n- stale cache or cache without explicit invalidation;\n- repeated rediscovery during retries;\n- excessive work per pointermove/keystroke/frame;\n- resize/maximize/restore/focus/blur causing stale geometry or targets.\n\nASYNC / STABILITY\n- double submit/dispatch/callback;\n- stale async result overwriting newer state;\n- ignored cancellation;\n- work continuing after target/state changed;\n- retry storms or excessive retry chains;\n- timer polling where a reliable event exists;\n- teardown while work is pending;\n- nondeterministic repeated-operation ordering.\n\nMEMORY / RESOURCE BOUNDS\n- unbounded queues, arrays, maps, sets, logs, buffers or caches;\n- retained detached objects/nodes;\n- duplicate large strings/data;\n- resources not released over long sessions.\n\nI/O / STARTUP\n- repeated read/write of unchanged local state;\n- full-state serialization for tiny changes;\n- expensive local I/O in hot UI paths;\n- repeated hashing/path resolution of stable data;\n- eager loading/scanning that can safely be lazy.\n\nEFFECTIVENESS\n- duplicated hot-path branches that can become one canonical path;\n- unnecessary abstraction/indirection adding material work;\n- complex execution where a smaller path preserves semantics;\n- cache only stable data with explicit invalidation;\n- prefer event-driven work over polling when behavior stays deterministic.\n\nEVIDENCE CLASS\n\nEvery ticket must be exactly one:\n\nPROVEN BOTTLENECK = directly measured or demonstrated from available evidence.\nSTRONGLY EVIDENCED WASTE = execution structure clearly performs unnecessary material work.\nLOW-RISK SIMPLIFICATION = behavior-preserving simplification with credible latency/stability/effectiveness benefit.\n\nNever fabricate timings.\nUse PARTIAL only for a genuine execution/context limit. PARTIAL is a resumable machine checkpoint, not a request for human intervention; expect an automatic same-wave continuation. If measurement is unavailable, prescribe an exact local benchmark/regression method.\n\nDo not interact with or load-test external systems.\n\nTEST EXECUTION / ENVIRONMENT LIMITS\n\nTreat test execution as evidence, not as an artificial gate that can invalidate an otherwise useful audit.\n\nClassify verification truthfully:\n- TEST_PASSED = the relevant intended tests/checks actually ran and passed.\n- TEST_FAILED = a relevant test/check actually ran and failed because of project behavior.\n- TEST_PARTIAL = only part of the relevant intended verification could run.\n- TEST_NOT_RUN_ENVIRONMENT = the intended relevant tests could not start/run because this execution environment lacks an external prerequisite.\n- TEST_NOT_APPLICABLE = there is no meaningful runnable test/check for this audited surface.\n\nEnvironment limitations include unavailable third-party packages, GUI/runtime/display facilities, system libraries, platform capabilities, network/package-install access, toolchains, services, or other prerequisites outside the supplied project.\n\nRules:\n- A dependency that the project correctly declares but this sandbox does not provide is an ENVIRONMENT LIMITATION, not a project defect.\n- If the implementation requires a dependency but the project itself fails to declare/package/document it where its normal runtime/build contract requires that declaration, that may be a real project defect when verified.\n- Do not fabricate a red/green test result for a command that never actually ran.\n- Do not label the whole audit BLOCKED merely because the complete test suite cannot execute. Continue with every meaningful verification still available.\n- Run unaffected tests/checks that do not require the missing prerequisite when practical.\n- Use static inspection, compile/type/syntax checks, focused pure-logic tests, fixture analysis, or other local verification as substitutes where they materially increase confidence.\n- Record the exact unavailable prerequisite and the exact command/error that prevented execution when known.\n- Reduce confidence only for findings whose verification materially depends on the unavailable capability.\n- Do not weaken, skip, rewrite, or make legitimate project tests optional merely to accommodate this sandbox.\n- Do not repeatedly burn audit budget trying the same unavailable installation/setup path. At most one bounded setup attempt is appropriate when the environment clearly supports it and the project declares the setup path.\n- Missing environment capability must never become a fake project ticket merely to make the audit look complete.\n- PARTIAL audit status is for an actually unfinished AUDIT WAVE due to execution/context budget, not merely for partial TEST COVERAGE. A wave may be STATUS ... COMPLETE while TEST_STATUS is TEST_PARTIAL or TEST_NOT_RUN_ENVIRONMENT if the code audit itself is complete and the limitation is explicitly recorded.\n\nNO EARLIER-WAVE REHASH\n\nDo not repeat Core/Second findings unless performance/stability analysis materially changes their root cause or required repair.\n\nFINAL HANDOFF\n\nReturn ONE code block only, standalone for a separate implementation agent.\n\nHeader exactly:\n\nPROJECT_NAME: <name>\nDATE_TIME: <current session/local date-time when available; otherwise UTC>\nWAVE: AUDIT PERFORMANCE / STABILITY / EFFECTIVENESS\nTARGET: <artifact/project inspected>\nBASELINE: <current identity>\nPREVIOUS_BASELINE: <Second Wave identity>\nGIT_CONTEXT: <PRESENT branch@commit | ABSENT | UNREADABLE> - <brief note>\nSAIPEN_CONTEXT: <PRESENT | ABSENT | STALE | UNREADABLE> - <brief useful note>\nAUDIT_SCOPE: <compact hot paths/areas inspected>\nTEST_STATUS: <TEST_PASSED | TEST_FAILED | TEST_PARTIAL | TEST_NOT_RUN_ENVIRONMENT | TEST_NOT_APPLICABLE>\nTEST_LIMITATION: <NONE | exact unavailable prerequisite + command/error>\nVERIFIED_INSTEAD: <NONE | concise alternative verification actually performed>\nSTATUS: PERFORMANCE: <COMPLETE | PARTIAL | BLOCKED>\nTICKETS: <count>\nHANDOFF: IMPLEMENTATION_AGENT\n\nThen only material findings:\n\n[P0|P1|P2] [PERF-001] <PROVEN BOTTLENECK|STRONGLY EVIDENCED WASTE|LOW-RISK SIMPLIFICATION> <path/module/symbol>\nEVIDENCE: <specific hot-path/stability evidence>\nISSUE: <verified waste/root cause and consequence>\nOPTIMIZE: <smallest behavior-preserving change>\nGUARDRAIL: <correct behavior that must remain unchanged>\nVERIFY: <benchmark/regression procedure>\n\nUse PERF-001, PERF-002... only within this handoff.\n\nIf no material findings exist:\nTICKETS: 0\nNO MATERIAL PERFORMANCE/STABILITY FINDINGS.\n\nEnd:\nPERFORMANCE_DONE_WHEN: <compact latency/stability/behavior verification gate>\n\nNo prose outside the code block.";

  const BUILTIN_PRESETS = [
    {
      builtinId: 'audit-core-v8-unattended',
      legacyIds: ['audit-core-v7-handoff', 'audit-core-v6-audit-first', 'audit-core-v4-saipen-native', 'audit-core-v3-saipen', 'audit-core-v2-quality', 'audit-core-v1'],
      name: 'Audit Core',
      desc: 'Deep correctness audit -> implementation handoff',
      text: AUDIT_CORE
    },
    {
      builtinId: 'audit-second-wave-v8-unattended',
      legacyIds: ['audit-second-wave-v7-handoff', 'audit-second-wave-v6-audit-first', 'audit-second-wave-v4-saipen-native', 'audit-second-wave-v3-saipen', 'audit-second-wave-v2-quality', 'audit-second-wave-v1'],
      name: 'Audit Second Wave',
      desc: 'Complementary second lens -> implementation handoff',
      text: AUDIT_SECOND_WAVE
    },
    {
      builtinId: 'audit-performance-v8-unattended',
      legacyIds: ['audit-performance-v7-handoff', 'audit-performance-v6-audit-first', 'audit-performance-v4-saipen-native', 'audit-performance-v3-saipen', 'audit-performance-v2-quality', 'audit-performance-v1'],
      name: 'Audit Performance',
      desc: 'Performance/stability closing lens -> handoff',
      text: AUDIT_PERFORMANCE
    }
  ];

  const CSS = `
#acb-popup {
  --background:#1A1810;
  --backgroundSoft:#232018;
  --surface:#332E22;
  --surfaceRaised:#3D372A;
  --surfaceAlt:#453D30;
  --borderDark:#100E08;
  --borderHighlight:#F0D060;
  --bevelLight:#75663D;
  --borderMuted:#5A5040;
  --textPrimary:#D4C89A;
  --textSecondary:#9C9371;
  --textMuted:#6E674E;
  --accentTeal:#008080;
  --accentTealDeep:#004C4C;
  --success:#4A7A20;
  --warning:#7A7A20;
  --danger:#7A2020;
  --dangerText:#D66464;
  --selection:#3D372A;
  --compareBack:#14120C;
  --link:#F0D060;
}

#acb-popup,
#acb-popup * {
  font-family: Verdana, sans-serif !important;
  -webkit-font-smoothing: none !important;
  -moz-osx-font-smoothing: unset !important;
  font-smooth: never !important;
  text-rendering: optimizeSpeed !important;
  border-radius: 0 !important;
  transition: none !important;
  animation: none !important;
  box-shadow: none !important;
  text-shadow: none !important;
  box-sizing: border-box !important;
}

#acb-popup {
  position: fixed !important;
  z-index: 2147483646 !important;
  width: ${PANEL_WIDTH}px !important;
  height: ${PANEL_HEIGHT}px !important;
  max-width: calc(100vw - 16px) !important;
  max-height: calc(100vh - 16px) !important;
  display: flex !important;
  flex-direction: column !important;
  margin: 0 !important;
  padding: 0 !important;
  overflow: hidden !important;
  background: var(--background) !important;
  color: var(--textPrimary) !important;
  border: 2px solid !important;
  border-color: var(--bevelLight) var(--borderDark) var(--borderDark) var(--bevelLight) !important;
  font-size: 12px !important;
  line-height: 1.2 !important;
}

#acb-titlebar {
  height: 24px !important;
  min-height: 24px !important;
  display: flex !important;
  align-items: center !important;
  gap: 6px !important;
  padding: 2px 3px !important;
  background: var(--surface) !important;
  color: var(--textPrimary) !important;
  border-bottom: 2px solid var(--borderDark) !important;
  user-select: none !important;
  touch-action: none !important;
}
#acb-titlebar.acb-movable { cursor: move !important; }
#acb-title {
  flex: 1 1 auto !important;
  min-width: 0 !important;
  font-size: 12px !important;
  font-weight: 700 !important;
  white-space: nowrap !important;
  overflow: hidden !important;
  text-overflow: ellipsis !important;
}
#acb-site {
  flex: 0 1 auto !important;
  max-width: 92px !important;
  color: var(--textSecondary) !important;
  font-size: 10px !important;
  white-space: nowrap !important;
  overflow: hidden !important;
  text-overflow: ellipsis !important;
}


#acb-super-toggle {
  flex: 0 0 auto !important;
  min-width: 38px !important;
  min-height: 18px !important;
  height: 18px !important;
  padding: 1px 4px !important;
  font-size: 10px !important;
}

#acb-super-controls {
  display: none !important;
  flex: 1 1 auto !important;
  min-width: 0 !important;
  height: 20px !important;
  align-items: center !important;
  gap: 3px !important;
  overflow: hidden !important;
}


#acb-popup #acb-new-chat {
  flex: 0 0 18px !important;
  width: 18px !important;
  min-width: 18px !important;
  max-width: 18px !important;
  height: 18px !important;
  min-height: 18px !important;
  max-height: 18px !important;
  margin: 0 !important;
  padding: 0 !important;
  border-width: 1px !important;
  font-size: 14px !important;
  line-height: 16px !important;
  font-weight: 700 !important;
  overflow: hidden !important;
}

#acb-super-brand {
  flex: 1 1 82px !important;
  width: auto !important;
  min-width: 30px !important;
  max-width: 100px !important;
  color: var(--textPrimary) !important;
  font-size: 10px !important;
  font-weight: 700 !important;
  text-align: left !important;
  white-space: nowrap !important;
  overflow: hidden !important;
  text-overflow: ellipsis !important;
}

#acb-super-auto-label {
  flex: 0 0 18px !important;
  width: 18px !important;
  height: 18px !important;
  display: flex !important;
  align-items: center !important;
  justify-content: center !important;
  margin: 0 !important;
  padding: 0 !important;
  cursor: pointer !important;
}

#acb-popup #acb-super-enabled {
  width: 14px !important;
  min-width: 14px !important;
  max-width: 14px !important;
  height: 14px !important;
  min-height: 14px !important;
  margin: 0 !important;
  padding: 0 !important;
  accent-color: var(--accentTeal) !important;
  appearance: auto !important;
  border: 0 !important;
  background: transparent !important;
}

#acb-super-progress {
  flex: 0 0 66px !important;
  width: 66px !important;
  height: 18px !important;
  display: grid !important;
  grid-template-columns: repeat(3, 1fr) !important;
  gap: 2px !important;
}

.acb-super-step {
  min-width: 0 !important;
  height: 18px !important;
  display: flex !important;
  align-items: center !important;
  justify-content: center !important;
  padding: 0 !important;
  background: var(--compareBack) !important;
  color: var(--textMuted) !important;
  border: 1px solid var(--borderMuted) !important;
  font-size: 10px !important;
  font-weight: 700 !important;
  line-height: 1 !important;
}

.acb-super-step[data-state="active"] {
  background: var(--selection) !important;
  color: var(--borderHighlight) !important;
  border-color: var(--bevelLight) !important;
}

.acb-super-step[data-state="done"] {
  background: var(--surface) !important;
  color: var(--textPrimary) !important;
}

#acb-super-state {
  flex: 1 1 auto !important;
  min-width: 42px !important;
  max-width: 78px !important;
  height: 18px !important;
  display: flex !important;
  align-items: center !important;
  padding: 0 3px !important;
  overflow: hidden !important;
  white-space: nowrap !important;
  text-overflow: ellipsis !important;
  background: var(--compareBack) !important;
  color: var(--textSecondary) !important;
  border: 1px solid var(--borderMuted) !important;
  font-size: 9px !important;
  line-height: 1 !important;
}

#acb-super-state[data-kind="success"] { color: var(--textPrimary) !important; }
#acb-super-state[data-kind="warning"] { color: var(--borderHighlight) !important; }
#acb-super-state[data-kind="error"] { color: var(--dangerText) !important; }

#acb-popup[data-supercompact="true"] #acb-titlebar {
  height: 24px !important;
  min-height: 24px !important;
  gap: 3px !important;
  padding: 2px 3px !important;
  border-bottom: 0 !important;
}

#acb-popup[data-supercompact="true"] #acb-title,
#acb-popup[data-supercompact="true"] #acb-site,
#acb-popup[data-supercompact="true"] #acb-collapse,
#acb-popup[data-supercompact="true"] #acb-tabs,
#acb-popup[data-supercompact="true"] #acb-content,
#acb-popup[data-supercompact="true"] #acb-status {
  display: none !important;
}

#acb-popup[data-supercompact="true"] #acb-super-controls {
  display: flex !important;
}

#acb-popup[data-supercompact="true"] #acb-super-toggle {
  min-width: 36px !important;
  width: 36px !important;
  height: 18px !important;
  min-height: 18px !important;
  padding: 1px 3px !important;
}

#acb-popup button,
#acb-popup .acb-buttonlike {
  min-width: 30px !important;
  min-height: 24px !important;
  margin: 0 !important;
  padding: 3px 7px !important;
  border: 2px solid !important;
  border-color: var(--bevelLight) var(--borderDark) var(--borderDark) var(--bevelLight) !important;
  background: var(--surfaceRaised) !important;
  color: var(--textPrimary) !important;
  cursor: pointer !important;
  font-size: 11px !important;
  line-height: 1.1 !important;
  text-align: center !important;
}
#acb-popup button:hover,
#acb-popup .acb-buttonlike:hover { background: var(--surfaceAlt) !important; }
#acb-popup button:active,
#acb-popup button.acb-active,
#acb-popup .acb-buttonlike:active {
  border-color: var(--borderDark) var(--bevelLight) var(--bevelLight) var(--borderDark) !important;
  background: var(--surface) !important;
  transform: translate(1px, 1px) !important;
}
#acb-popup button.acb-active { transform: none !important; }
#acb-popup button:focus-visible,
#acb-popup input:focus-visible,
#acb-popup select:focus-visible,
#acb-popup textarea:focus-visible,
#acb-popup .acb-buttonlike:focus-visible {
  outline: 1px dotted var(--textPrimary) !important;
  outline-offset: -4px !important;
}
#acb-popup button:disabled {
  color: var(--textMuted) !important;
  background: var(--surfaceRaised) !important;
  cursor: default !important;
}


/* SUPER COMPACT GEOMETRY ISOLATION
   Keep this selector more specific than the generic #acb-popup button rule.
   The mini monitor must never inherit full-size button geometry. */
#acb-popup[data-supercompact="true"] #acb-super-progress {
  flex: 0 0 72px !important;
  width: 72px !important;
  min-width: 72px !important;
  max-width: 72px !important;
  height: 18px !important;
  display: grid !important;
  grid-template-columns: repeat(3, minmax(0, 1fr)) !important;
  gap: 2px !important;
  overflow: visible !important;
}

#acb-popup[data-supercompact="true"] #acb-super-progress > .acb-super-step {
  box-sizing: border-box !important;
  min-width: 0 !important;
  max-width: none !important;
  width: 100% !important;
  min-height: 18px !important;
  max-height: 18px !important;
  height: 18px !important;
  margin: 0 !important;
  padding: 0 !important;
  border-width: 1px !important;
  font-size: 10px !important;
  line-height: 16px !important;
  overflow: hidden !important;
  white-space: nowrap !important;
  transform: none !important;
}

#acb-popup[data-supercompact="true"] #acb-super-progress > .acb-super-step:active {
  transform: translate(1px, 1px) !important;
}

#acb-popup[data-supercompact="true"] #acb-super-state {
  flex: 1 1 48px !important;
  min-width: 42px !important;
  max-width: 78px !important;
}

#acb-popup[data-supercompact="true"] #acb-super-toggle {
  flex: 0 0 36px !important;
  min-width: 36px !important;
  max-width: 36px !important;
  width: 36px !important;
  box-sizing: border-box !important;
}

#acb-collapse {
  flex: 0 0 auto !important;
  min-width: 60px !important;
  min-height: 18px !important;
  height: 18px !important;
  padding: 1px 5px !important;
  font-size: 10px !important;
}

#acb-popup[data-collapsed="true"] #acb-tabs,
#acb-popup[data-collapsed="true"] #acb-content,
#acb-popup[data-collapsed="true"] #acb-status {
  display: none !important;
}
#acb-popup[data-collapsed="true"] #acb-titlebar { border-bottom: 0 !important; }

#acb-tabs {
  height: 30px !important;
  min-height: 30px !important;
  display: grid !important;
  grid-template-columns: repeat(3, 1fr) !important;
  gap: 2px !important;
  padding: 3px !important;
  background: var(--backgroundSoft) !important;
  border-bottom: 2px solid var(--borderDark) !important;
}
#acb-tabs button {
  min-height: 24px !important;
  padding: 2px 4px !important;
  font-size: 11px !important;
  font-weight: 700 !important;
}
#acb-tabs button[aria-selected="true"] {
  border-color: var(--borderDark) var(--bevelLight) var(--bevelLight) var(--borderDark) !important;
  background: var(--selection) !important;
  color: var(--borderHighlight) !important;
}

#acb-content {
  flex: 1 1 auto !important;
  min-height: 0 !important;
  overflow: hidden !important;
  padding: 5px !important;
  background: var(--background) !important;
}

.acb-view {
  width: 100% !important;
  height: 100% !important;
  min-height: 0 !important;
  background: var(--background) !important;
}
.acb-view[hidden] { display: none !important; }
.acb-view-scroll {
  overflow-y: auto !important;
  overflow-x: hidden !important;
  padding-right: 1px !important;
}
#acb-view-commands {
  display: flex !important;
  flex-direction: column !important;
  gap: 5px !important;
  overflow: hidden !important;
}

.acb-section {
  margin: 0 0 6px 0 !important;
  padding: 5px !important;
  background: var(--backgroundSoft) !important;
  border: 1px solid var(--borderMuted) !important;
}
.acb-section:last-child { margin-bottom: 0 !important; }
.acb-section-title {
  margin: 0 0 4px 0 !important;
  color: var(--textPrimary) !important;
  font-size: 12px !important;
  font-weight: 700 !important;
}
.acb-section-note {
  color: var(--textMuted) !important;
  font-size: 10px !important;
  line-height: 1.25 !important;
}
.acb-label {
  display: block !important;
  margin: 0 0 2px 0 !important;
  color: var(--textSecondary) !important;
  font-size: 10px !important;
}

#acb-popup input,
#acb-popup select,
#acb-popup textarea {
  width: 100% !important;
  margin: 0 !important;
  border: 2px solid !important;
  border-color: var(--borderDark) var(--bevelLight) var(--bevelLight) var(--borderDark) !important;
  background: var(--compareBack) !important;
  color: var(--textPrimary) !important;
  font-size: 11px !important;
  outline: none !important;
}
#acb-popup input,
#acb-popup select { height: 24px !important; padding: 2px 4px !important; }
#acb-popup textarea {
  min-height: 120px !important;
  height: 120px !important;
  padding: 4px !important;
  resize: vertical !important;
}
#acb-popup input.acb-error,
#acb-popup textarea.acb-error { border-color: var(--danger) !important; color: var(--dangerText) !important; }


/* COMPACT CHECKBOXES: never inherit text-input geometry. */
#acb-popup input[type="checkbox"] {
  appearance: none !important;
  -webkit-appearance: none !important;
  position: relative !important;
  flex: 0 0 11px !important;
  width: 11px !important;
  min-width: 11px !important;
  max-width: 11px !important;
  height: 11px !important;
  min-height: 11px !important;
  max-height: 11px !important;
  margin: 0 !important;
  padding: 0 !important;
  border: 1px solid var(--bevelLight) !important;
  background: var(--compareBack) !important;
  color: var(--borderHighlight) !important;
  cursor: pointer !important;
}
#acb-popup input[type="checkbox"]:checked {
  background: var(--accentTealDeep) !important;
  border-color: var(--accentTeal) !important;
}
#acb-popup input[type="checkbox"]:checked::after {
  content: "✓" !important;
  position: absolute !important;
  inset: -1px 0 0 0 !important;
  display: flex !important;
  align-items: center !important;
  justify-content: center !important;
  color: var(--borderHighlight) !important;
  font-family: Verdana, sans-serif !important;
  font-size: 9px !important;
  font-weight: 700 !important;
  line-height: 10px !important;
}
#acb-popup input[type="checkbox"]:focus-visible {
  outline: 1px solid var(--borderHighlight) !important;
  outline-offset: 1px !important;
}
#acb-popup .acb-check-row {
  min-height: 17px !important;
  display: flex !important;
  align-items: center !important;
  gap: 4px !important;
  padding: 1px 2px !important;
  background: transparent !important;
  border: 0 !important;
  color: var(--textPrimary) !important;
  font-size: 10px !important;
  cursor: pointer !important;
}
#acb-auto-head {
  grid-template-columns: minmax(66px, 1fr) 52px 58px 52px !important;
}
#acb-auto-toggle-label,
#acb-auto-save-label {
  min-width: 0 !important;
  min-height: 24px !important;
  height: 24px !important;
  display: flex !important;
  align-items: center !important;
  justify-content: flex-start !important;
  gap: 4px !important;
  padding: 2px 4px !important;
  color: var(--textPrimary) !important;
  background: var(--surfaceRaised) !important;
  border: 1px solid var(--borderMuted) !important;
  font-size: 10px !important;
  font-weight: 700 !important;
  cursor: pointer !important;
  overflow: hidden !important;
  white-space: nowrap !important;
}
#acb-auto-adopt,
#acb-auto-stop {
  min-height: 24px !important;
  height: 24px !important;
}
#acb-auto-state-row {
  display: grid !important;
  grid-template-columns: minmax(0, 1fr) 52px !important;
  gap: 3px !important;
  margin-top: 4px !important;
}
#acb-auto-state-row #acb-auto-state { margin-top: 0 !important; }
#acb-save-now {
  min-width: 0 !important;
  min-height: 24px !important;
  height: 24px !important;
  padding: 2px 3px !important;
  font-size: 10px !important;
  font-weight: 700 !important;
}
#acb-save-now[data-state="pending"] {
  background: var(--accentTealDeep) !important;
  color: var(--borderHighlight) !important;
  border-color: var(--accentTeal) !important;
}
#acb-save-now[data-state="error"] {
  background: var(--danger) !important;
  color: var(--dangerText) !important;
  border-color: var(--dangerText) !important;
}
#acb-save-now[data-state="saved"] {
  background: var(--success) !important;
  color: var(--textPrimary) !important;
}
#acb-audit-copy-hint {
  margin-top: 3px !important;
  color: var(--textMuted) !important;
  font-size: 9px !important;
  line-height: 1.2 !important;
}
.acb-auto-step[data-copy-ready="true"],
.acb-super-step[data-copy-ready="true"] { cursor: copy !important; }
.acb-auto-step[data-copied="true"],
.acb-super-step[data-copied="true"] {
  background: var(--accentTealDeep) !important;
  color: var(--borderHighlight) !important;
  border-color: var(--accentTeal) !important;
}
#acb-popup[data-supercompact="true"] #acb-super-auto-label,
#acb-popup[data-supercompact="true"] #acb-super-save-label {
  flex: 0 0 auto !important;
  width: auto !important;
  min-width: 0 !important;
  height: 18px !important;
  display: flex !important;
  align-items: center !important;
  justify-content: center !important;
  gap: 2px !important;
  margin: 0 !important;
  padding: 0 1px !important;
  color: var(--textSecondary) !important;
  font-size: 9px !important;
  font-weight: 700 !important;
  cursor: pointer !important;
  white-space: nowrap !important;
}
#acb-popup[data-supercompact="true"] #acb-super-state {
  min-width: 42px !important;
  min-height: 18px !important;
  max-height: 18px !important;
  height: 18px !important;
  margin: 0 !important;
  padding: 0 3px !important;
  border-width: 1px !important;
  font-size: 9px !important;
  line-height: 16px !important;
  cursor: pointer !important;
}
#acb-popup[data-supercompact="true"] #acb-super-state:active {
  transform: translate(1px, 1px) !important;
}
#acb-popup #acb-super-enabled,
#acb-popup #acb-super-save,
#acb-popup #acb-auto-enabled,
#acb-popup #acb-auto-save-run {
  width: 11px !important;
  min-width: 11px !important;
  max-width: 11px !important;
  height: 11px !important;
  min-height: 11px !important;
  max-height: 11px !important;
  padding: 0 !important;
  margin: 0 !important;
  appearance: none !important;
  -webkit-appearance: none !important;
}

/* RUN: automation stays visible, configuration does not. */
#acb-auto-audit {
  flex: 0 0 auto !important;
  margin: 0 !important;
  padding: 5px !important;
  background: var(--backgroundSoft) !important;
  border: 1px solid var(--borderMuted) !important;
}
#acb-auto-head {
  display: grid !important;
  grid-template-columns: minmax(0, 1fr) 72px 62px !important;
  gap: 3px !important;
  align-items: center !important;
}
#acb-auto-toggle-label {
  min-height: 28px !important;
  display: flex !important;
  align-items: center !important;
  gap: 6px !important;
  padding: 3px 5px !important;
  color: var(--textPrimary) !important;
  background: var(--surfaceRaised) !important;
  border: 1px solid var(--borderMuted) !important;
  font-size: 11px !important;
  font-weight: 700 !important;
  cursor: pointer !important;
}
#acb-auto-enabled {
  width: 15px !important;
  min-width: 15px !important;
  height: 15px !important;
  min-height: 15px !important;
  padding: 0 !important;
  margin: 0 !important;
  accent-color: var(--accentTeal) !important;
  appearance: auto !important;
}
#acb-auto-adopt,
#acb-auto-stop {
  min-width: 0 !important;
  min-height: 28px !important;
  padding: 2px 4px !important;
  font-size: 10px !important;
}
#acb-auto-progress {
  display: grid !important;
  grid-template-columns: repeat(3, 1fr) !important;
  gap: 2px !important;
  margin-top: 4px !important;
}
.acb-auto-step {
  min-width: 0 !important;
  padding: 3px 2px !important;
  text-align: center !important;
  background: var(--compareBack) !important;
  color: var(--textMuted) !important;
  border: 1px solid var(--borderMuted) !important;
  font-size: 10px !important;
  white-space: nowrap !important;
  overflow: hidden !important;
  text-overflow: ellipsis !important;
}
.acb-auto-step[data-state="active"] {
  background: var(--selection) !important;
  color: var(--borderHighlight) !important;
  border-color: var(--bevelLight) !important;
}
.acb-auto-step[data-state="done"] {
  background: var(--surface) !important;
  color: var(--textPrimary) !important;
}
#acb-auto-state {
  min-height: 30px !important;
  max-height: 42px !important;
  margin-top: 4px !important;
  padding: 3px 4px !important;
  overflow: hidden !important;
  background: var(--compareBack) !important;
  color: var(--textSecondary) !important;
  border: 1px solid var(--borderMuted) !important;
  font-size: 10px !important;
  line-height: 1.2 !important;
}
#acb-auto-state[data-kind="success"] { color: var(--textPrimary) !important; }
#acb-auto-state[data-kind="warning"] { color: var(--borderHighlight) !important; }
#acb-auto-state[data-kind="error"] { color: var(--dangerText) !important; }

/* RUN: the three audit waves are permanently pinned. */
#acb-audit-quick {
  flex: 0 0 auto !important;
  margin: 0 !important;
  padding: 5px !important;
  background: var(--backgroundSoft) !important;
  border: 1px solid var(--borderMuted) !important;
}
#acb-audit-quick-list {
  display: flex !important;
  flex-direction: column !important;
  gap: 3px !important;
}
.acb-audit-quick-row {
  display: grid !important;
  grid-template-columns: 34px minmax(0, 1fr) 70px 54px !important;
  gap: 3px !important;
  align-items: center !important;
  min-height: 42px !important;
  padding: 3px !important;
  background: var(--surface) !important;
  border: 1px solid var(--borderMuted) !important;
}
.acb-audit-wave-index {
  display: flex !important;
  align-items: center !important;
  justify-content: center !important;
  height: 30px !important;
  font-size: 14px !important;
  font-weight: 700 !important;
  color: var(--borderHighlight) !important;
  background: var(--compareBack) !important;
  border: 1px solid var(--borderMuted) !important;
}
.acb-audit-wave-copy { min-width: 0 !important; }
.acb-audit-wave-name {
  color: var(--textPrimary) !important;
  font-size: 11px !important;
  font-weight: 700 !important;
  white-space: nowrap !important;
  overflow: hidden !important;
  text-overflow: ellipsis !important;
}
.acb-audit-wave-desc {
  margin-top: 2px !important;
  color: var(--textMuted) !important;
  font-size: 9px !important;
  white-space: nowrap !important;
  overflow: hidden !important;
  text-overflow: ellipsis !important;
}
.acb-audit-quick-row button {
  min-width: 0 !important;
  min-height: 30px !important;
  padding: 2px 4px !important;
  font-size: 10px !important;
}

/* Other/custom commands use the remaining space only when they exist. */
#acb-other-commands {
  flex: 1 1 auto !important;
  min-height: 0 !important;
  margin: 0 !important;
  padding: 5px !important;
  overflow: hidden !important;
  background: var(--backgroundSoft) !important;
  border: 1px solid var(--borderMuted) !important;
}
#acb-other-commands[hidden] { display: none !important; }
#acb-command-tools {
  display: grid !important;
  grid-template-columns: minmax(0, 1fr) minmax(120px, 0.8fr) !important;
  gap: 3px !important;
  margin-bottom: 4px !important;
}
#acb-catbar {
  min-width: 0 !important;
  display: flex !important;
  gap: 2px !important;
  overflow-x: auto !important;
  overflow-y: hidden !important;
  white-space: nowrap !important;
}
#acb-catbar[hidden] { display: none !important; }
#acb-catbar button {
  flex: 0 0 auto !important;
  min-width: 64px !important;
  min-height: 24px !important;
  padding: 2px 5px !important;
  font-size: 10px !important;
}
#acb-catbar button[aria-selected="true"] {
  border-color: var(--borderDark) var(--bevelLight) var(--bevelLight) var(--borderDark) !important;
  background: var(--selection) !important;
}
#acb-filter-wrap { min-width: 0 !important; }
#acb-filter-wrap[hidden] { display: none !important; }
#acb-filter { height: 24px !important; }
#acb-command-list {
  height: calc(100% - 30px) !important;
  min-height: 0 !important;
  overflow-y: auto !important;
  display: flex !important;
  flex-direction: column !important;
  gap: 3px !important;
}
.acb-command-row {
  display: grid !important;
  grid-template-columns: minmax(0, 1fr) 70px 54px !important;
  gap: 3px !important;
  align-items: center !important;
  min-height: 34px !important;
  padding: 3px !important;
  background: var(--surface) !important;
  border: 1px solid var(--borderMuted) !important;
}
.acb-command-name {
  min-width: 0 !important;
  color: var(--textPrimary) !important;
  font-size: 11px !important;
  overflow: hidden !important;
  white-space: nowrap !important;
  text-overflow: ellipsis !important;
}
.acb-command-row button {
  min-width: 0 !important;
  min-height: 28px !important;
  padding: 2px 4px !important;
  font-size: 10px !important;
}
.acb-empty {
  padding: 8px 5px !important;
  color: var(--textMuted) !important;
  font-size: 11px !important;
  text-align: center !important;
  border: 1px solid var(--borderMuted) !important;
  background: var(--backgroundSoft) !important;
}

/* EDIT */
.acb-row {
  display: flex !important;
  gap: 3px !important;
  align-items: center !important;
  margin-top: 4px !important;
}
.acb-row > * { flex: 1 1 0 !important; min-width: 0 !important; }
#acb-manage-category { margin-bottom: 2px !important; }
#acb-manage-list {
  display: flex !important;
  flex-direction: column !important;
  gap: 3px !important;
}
.acb-manage-row {
  padding: 3px !important;
  background: var(--surface) !important;
  border: 1px solid var(--borderMuted) !important;
}
.acb-manage-name {
  margin-bottom: 3px !important;
  font-size: 11px !important;
  color: var(--textPrimary) !important;
  white-space: nowrap !important;
  overflow: hidden !important;
  text-overflow: ellipsis !important;
}
.acb-manage-actions {
  display: grid !important;
  grid-template-columns: repeat(4, 1fr) !important;
  gap: 2px !important;
}
.acb-manage-actions button { min-width: 0 !important; padding: 2px 3px !important; font-size: 10px !important; }
#acb-editor[hidden] { display: none !important; }
.acb-field { margin-bottom: 5px !important; }
#acb-editor-actions { display: flex !important; gap: 3px !important; }
#acb-editor-actions button { flex: 1 1 0 !important; }
#acb-confirm-text {
  min-height: 40px !important;
  padding: 4px !important;
  overflow-y: auto !important;
  background: var(--compareBack) !important;
  color: var(--textSecondary) !important;
  border: 1px solid var(--borderMuted) !important;
  font-size: 10px !important;
}


/* AUDIT STATUS COLORS + COPYABLE WAVES */
.acb-auto-step,
.acb-super-step {
  cursor: pointer !important;
  user-select: none !important;
}
.acb-auto-step[data-state="active"],
.acb-super-step[data-state="active"] {
  background: var(--warning) !important;
  color: var(--textPrimary) !important;
  border-color: var(--borderHighlight) !important;
}
.acb-auto-step[data-state="done"],
.acb-super-step[data-state="done"] {
  background: var(--success) !important;
  color: var(--textPrimary) !important;
  border-color: var(--bevelLight) !important;
}
.acb-auto-step[data-state="recover"],
.acb-super-step[data-state="recover"] {
  background: var(--accentTealDeep) !important;
  color: var(--borderHighlight) !important;
  border-color: var(--accentTeal) !important;
}
.acb-auto-step[data-state="paused"],
.acb-super-step[data-state="paused"] {
  background: var(--danger) !important;
  color: var(--dangerText) !important;
  border-color: var(--dangerText) !important;
}
.acb-auto-step[data-copy-ready="true"],
.acb-super-step[data-copy-ready="true"] {
  text-decoration: underline !important;
  text-underline-offset: 2px !important;
}
#acb-super-state[data-state="done"] {
  background: var(--success) !important;
  color: var(--textPrimary) !important;
  border-color: var(--bevelLight) !important;
}
#acb-super-state[data-state="running"] {
  background: var(--warning) !important;
  color: var(--textPrimary) !important;
  border-color: var(--borderHighlight) !important;
}
#acb-super-state[data-state="recover"] {
  background: var(--accentTealDeep) !important;
  color: var(--borderHighlight) !important;
  border-color: var(--accentTeal) !important;
}
#acb-super-state[data-state="attention"] {
  background: var(--danger) !important;
  color: var(--dangerText) !important;
  border-color: var(--dangerText) !important;
}
#acb-super-state[data-state="standby"] {
  background: var(--surface) !important;
  color: var(--textSecondary) !important;
}

#acb-super-state[data-state="start"] {
  background: var(--accentTealDeep) !important;
  color: var(--borderHighlight) !important;
  border-color: var(--accentTeal) !important;
  font-weight: 700 !important;
}

#acb-super-state[data-state="start"]:disabled {
  background: var(--surfaceRaised) !important;
  color: var(--textMuted) !important;
  border-color: var(--borderMuted) !important;
}


/* AUDIT STAGE COLOR LADDER
   Golden Default only: CORE gold -> W2 olive -> PERF teal -> DONE green. */
.acb-auto-step[data-step="1"][data-state="active"],
.acb-super-step[data-step="1"][data-state="active"] {
  background: var(--surfaceAlt) !important;
  color: var(--borderHighlight) !important;
  border-color: var(--borderHighlight) !important;
}

.acb-auto-step[data-step="2"][data-state="active"],
.acb-super-step[data-step="2"][data-state="active"] {
  background: var(--warning) !important;
  color: var(--textPrimary) !important;
  border-color: var(--borderHighlight) !important;
}

.acb-auto-step[data-step="3"][data-state="active"],
.acb-super-step[data-step="3"][data-state="active"] {
  background: var(--accentTealDeep) !important;
  color: var(--borderHighlight) !important;
  border-color: var(--accentTeal) !important;
}

#acb-super-state[data-phase="core"]:not([data-state="attention"]),
#acb-auto-state[data-phase="core"] {
  background: var(--surfaceAlt) !important;
  color: var(--borderHighlight) !important;
  border-color: var(--borderHighlight) !important;
}

#acb-super-state[data-phase="second"]:not([data-state="attention"]),
#acb-auto-state[data-phase="second"] {
  background: var(--warning) !important;
  color: var(--textPrimary) !important;
  border-color: var(--borderHighlight) !important;
}

#acb-super-state[data-phase="performance"]:not([data-state="attention"]),
#acb-auto-state[data-phase="performance"] {
  background: var(--accentTealDeep) !important;
  color: var(--borderHighlight) !important;
  border-color: var(--accentTeal) !important;
}

#acb-super-state[data-phase="done"],
#acb-auto-state[data-phase="done"] {
  background: var(--success) !important;
  color: var(--textPrimary) !important;
  border-color: var(--bevelLight) !important;
}

/* HOLD keeps the underlying stage color; teal border means temporary recovery. */
#acb-super-state[data-hold="true"]:not([data-state="attention"]),
#acb-auto-state[data-hold="true"] {
  border-color: var(--accentTeal) !important;
}




/* ACBBridge */
#acb-bridge-config {
  display: grid !important;
  grid-template-columns: 1fr 1fr !important;
  gap: 4px !important;
}
#acb-bridge-config .acb-bridge-wide {
  grid-column: 1 / -1 !important;
}
#acb-bridge-config input[type="text"],
#acb-bridge-config input[type="password"] {
  width: 100% !important;
  min-width: 0 !important;
  height: 24px !important;
}
#acb-bridge-state {
  min-height: 22px !important;
  margin-top: 4px !important;
  padding: 3px 4px !important;
  background: var(--compareBack) !important;
  color: var(--textSecondary) !important;
  border: 1px solid var(--borderMuted) !important;
  font-size: 10px !important;
  overflow: hidden !important;
  white-space: nowrap !important;
  text-overflow: ellipsis !important;
}
#acb-bridge-state[data-state="ready"] {
  color: var(--textPrimary) !important;
  border-color: var(--success) !important;
}
#acb-bridge-state[data-state="error"] {
  color: var(--dangerText) !important;
  border-color: var(--danger) !important;
}
#acb-bridge-state[data-state="warning"] {
  color: var(--borderHighlight) !important;
  border-color: var(--warning) !important;
}
#acb-browser-fallback {
  margin-top: 6px !important;
  padding-top: 5px !important;
  border-top: 1px solid var(--borderMuted) !important;
}
#acb-audit-output-controls,
#acb-fallback-output-controls {
  display: grid !important;
  grid-template-columns: 1fr 1fr !important;
  gap: 4px !important;
}
.acb-check-row {
  min-height: 24px !important;
  display: flex !important;
  align-items: center !important;
  gap: 5px !important;
  padding: 3px 4px !important;
  background: var(--surface) !important;
  border: 1px solid var(--borderMuted) !important;
  color: var(--textPrimary) !important;
  font-size: 10px !important;
  cursor: pointer !important;
}
.acb-check-row input {
  width: 14px !important;
  min-width: 14px !important;
  height: 14px !important;
  min-height: 14px !important;
  padding: 0 !important;
  margin: 0 !important;
  appearance: auto !important;
  accent-color: var(--accentTeal) !important;
}
#acb-audit-folder-state {
  min-height: 26px !important;
  margin-top: 4px !important;
  padding: 3px 4px !important;
  background: var(--compareBack) !important;
  color: var(--textSecondary) !important;
  border: 1px solid var(--borderMuted) !important;
  font-size: 10px !important;
  overflow: hidden !important;
}
#acb-audit-folder-state[data-state="ready"] { color: var(--textPrimary) !important; border-color: var(--success) !important; }
#acb-audit-folder-state[data-state="warning"] { color: var(--borderHighlight) !important; border-color: var(--warning) !important; }
#acb-audit-folder-state[data-state="error"] { color: var(--dangerText) !important; border-color: var(--danger) !important; }
/* SETTINGS */
#acb-displaybar {
  display: grid !important;
  grid-template-columns: repeat(3, minmax(0, 1fr)) !important;
  gap: 4px !important;
}
.acb-display-field {
  min-width: 0 !important;
}
.acb-display-field label,
.acb-settings-field label,
.acb-auto-field label {
  display: block !important;
  margin: 0 0 2px 0 !important;
  color: var(--textSecondary) !important;
  font-size: 10px !important;
}
#acb-lock {
  align-self: end !important;
  min-width: 0 !important;
  height: 24px !important;
}
#acb-auto-config {
  display: grid !important;
  grid-template-columns: repeat(2, minmax(0, 1fr)) !important;
  gap: 5px !important;
}
.acb-auto-field { min-width: 0 !important; }
#acb-auto-reset {
  width: 100% !important;
  margin-top: 5px !important;
}
#acb-settings-data {
  display: grid !important;
  grid-template-columns: 1fr 1fr !important;
  gap: 4px !important;
}

/* Bottom status is a compact global message line, not another scrolling panel. */
#acb-status {
  min-height: 34px !important;
  height: 34px !important;
  display: grid !important;
  grid-template-columns: minmax(0, 1fr) 28px !important;
  gap: 3px !important;
  align-items: stretch !important;
  padding: 3px !important;
  background: var(--surfaceRaised) !important;
  border-top: 2px solid var(--borderDark) !important;
}
#acb-status-text {
  min-width: 0 !important;
  overflow: hidden !important;
  padding: 3px 4px !important;
  background: var(--compareBack) !important;
  color: var(--textSecondary) !important;
  border: 1px solid var(--borderMuted) !important;
  font-size: 10px !important;
  line-height: 1.2 !important;
  white-space: nowrap !important;
  text-overflow: ellipsis !important;
}
#acb-status-text[data-kind="success"] { color: var(--textPrimary) !important; }
#acb-status-text[data-kind="warning"] { color: var(--borderHighlight) !important; }
#acb-status-text[data-kind="error"] { color: var(--dangerText) !important; }
#acb-status button {
  min-width: 28px !important;
  width: 28px !important;
  padding: 1px !important;
  font-size: 12px !important;
}

#acb-popup ::selection { background: var(--selection) !important; color: var(--textPrimary) !important; }
#acb-popup a,
#acb-popup a:link,
#acb-popup a:visited { color: var(--link) !important; }
`;

  let state = null;
  let panel = null;
  let activeView = 'commands';
  let editingPresetId = null;
  let drag = null;
  let fileInput = null;
  let pendingAction = null;
  let actionInFlight = false;
  let viewportSyncFrame = 0;
  let dragFrame = 0;
  let autoAuditObserver = null;
  let autoAuditObserverRoot = null;
  let autoAuditObservedConfig = null;
  let autoAuditCheckTimer = 0;
  let autoAuditNextTimer = 0;
  let autoAuditEvaluating = false;
  let autoLastEvaluationAt = 0;

  let autoComposerHoldReason = '';
  let autoComposerHoldSince = 0;
  let autoComposerHoldAttempts = 0;
  let autoComposerHoldTimer = 0;
  let autoRuntime = null;
  let auditDirectoryHandle = null;
  let auditDirectoryState = 'unknown';
  let auditDirectoryMessage = 'Audit folder has not been checked yet.';

  let bridgeState = 'unknown';
  let bridgeMessage = 'Bridge has not been checked yet.';
  let bridgeOutputRoot = '';
  let bridgeServerVersion = '';
  let bridgeLastCheckedAt = 0;
  let bridgeFlushTimer = 0;
  let bridgeFlushInFlight = false;
  let bridgeQueueListenerId = null;
  let auditResultListenerId = null;

  let copiedAuditKind = '';
  let copiedAuditUntil = 0;
  let copiedAuditTimer = 0;

  let auditStartInFlight = false;
  let manualAuditSyncInFlight = false;
  let manualAuditSyncFeedback = '';
  let manualAuditSyncFeedbackUntil = 0;
  let manualAuditSyncFeedbackTimer = 0;
  let startRecoveryScheduleToken = '';

  let conversationTitleGuardToken = '';
  let conversationTitleGuardStartedAt = 0;
  let conversationTitleGuardProject = '';
  let conversationTitleGuardConversationKey = '';
  let conversationTitleGuardRunStartedAt = 0;
  let renameRateLimitNoticeAt = 0;
  let autoRouteTransientSince = 0;
  let lastLocalTitleApplyAt = 0;
  let projectTitleObserver = null;
  let projectTitleObserverLink = null;
  let projectTitleRepairTimer = 0;
  let widgetGuardianObserver = null;
  let widgetGuardianBody = null;
  let widgetBootstrapObserver = null;
  let widgetBootstrapTimers = [];

  // v0.0.26: Mini START must never render on every ChatGPT DOM mutation.
  // Keep a cheap attachment signature and only repaint when the actual
  // composer attachment state changes.
  let miniAttachmentSignature = '';
  let miniAttachmentRefreshTimer = 0;
  const auditResultCache = new Map();
  let autoBoundConversationKey = '';
  let autoRuntimeCorruptKey = '';
  let autoLeaseTimer = 0;
  const autoTabId = (() => {
    try {
      const existing = sessionStorage.getItem(AUTO_TAB_SESSION_KEY);
      if (existing) return existing;
    } catch (_) { }

    const created = globalThis.crypto && typeof globalThis.crypto.randomUUID === 'function'
      ? globalThis.crypto.randomUUID()
      : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;

    try { sessionStorage.setItem(AUTO_TAB_SESSION_KEY, created); } catch (_) { }
    return created;
  })();
  let autoDraftId = (() => {
    try {
      const existing = sessionStorage.getItem(AUTO_DRAFT_SESSION_KEY);
      if (existing) return existing;
    } catch (_) { }

    const created = globalThis.crypto && typeof globalThis.crypto.randomUUID === 'function'
      ? globalThis.crypto.randomUUID()
      : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
    try { sessionStorage.setItem(AUTO_DRAFT_SESSION_KEY, created); } catch (_) { }
    return created;
  })();
  const autoInstanceId = `${autoTabId}:${Math.random().toString(36).slice(2, 10)}`;
  const elementCache = { siteKey: '', input: null, send: null };

  function uid() {
    if (globalThis.crypto && typeof globalThis.crypto.randomUUID === 'function') {
      return globalThis.crypto.randomUUID();
    }
    return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
  }

  function escapeHTML(value) {
    return String(value ?? '').replace(/[&<>"']/g, ch => ({
      '&': '&amp;',
      '<': '&lt;',
      '>': '&gt;',
      '"': '&quot;',
      "'": '&#39;'
    }[ch]));
  }

  function trustedHTML(html) {
    if (!window.trustedTypes || !window.trustedTypes.createPolicy) return html;
    if (!window.__acbTrustedPolicy) {
      try {
        window.__acbTrustedPolicy = window.trustedTypes.createPolicy('acb-policy', {
          createHTML: value => value
        });
      } catch (_) {
        return html;
      }
    }
    return window.__acbTrustedPolicy.createHTML(html);
  }

  function setHTML(element, html) {
    element.innerHTML = trustedHTML(html);
  }

  function isVisible(element) {
    if (!element || !element.isConnected || element.hidden) return false;
    try {
      const rect = element.getBoundingClientRect();
      return rect.width > 0 || rect.height > 0;
    } catch (_) {
      return false;
    }
  }

  const MAX_SHADOW_SCAN = 800;

  function queryDeepFirst(selector, root = document) {
    let first = null;
    try {
      first = root.querySelector(selector);
      if (first && isVisible(first)) return first;
      if (first) {
        const directMatches = root.querySelectorAll(selector);
        for (const direct of directMatches) {
          if (isVisible(direct)) return direct;
        }
      }
    } catch (_) { }

    const start = root === document ? document.documentElement : root;
    if (!start) return null;
    const roots = [start];
    const seen = new Set();
    let scanned = 0;

    while (roots.length && scanned < MAX_SHADOW_SCAN) {
      const current = roots.shift();
      if (!current || seen.has(current)) continue;
      seen.add(current);

      let walker;
      try {
        walker = document.createTreeWalker(current, NodeFilter.SHOW_ELEMENT);
      } catch (_) {
        continue;
      }

      let element = current.nodeType === Node.ELEMENT_NODE ? current : walker.nextNode();
      while (element && scanned < MAX_SHADOW_SCAN) {
        scanned += 1;
        if (element.shadowRoot) {
          const shadow = element.shadowRoot;
          try {
            const hit = shadow.querySelector(selector);
            if (hit && isVisible(hit)) return hit;
          } catch (_) { }
          roots.push(shadow);
        }
        element = walker.nextNode();
      }
    }
    return null;
  }

  function dispatchInputEvent(element, data = null) {
    try {
      element.dispatchEvent(new InputEvent('input', {
        bubbles: true,
        cancelable: true,
        data,
        inputType: 'insertText'
      }));
    } catch (_) {
      try {
        element.dispatchEvent(new Event('input', { bubbles: true, cancelable: true }));
      } catch (_) { }
    }
  }

  function nativeSet(element, text) {
    if (!element) return false;
    try {
      element.focus({ preventScroll: true });
      const proto = element.tagName === 'TEXTAREA'
        ? HTMLTextAreaElement.prototype
        : element.tagName === 'INPUT'
          ? HTMLInputElement.prototype
          : null;
      if (proto) {
        const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
        if (setter) setter.call(element, text);
        else element.value = text;
      } else {
        element.textContent = text;
      }
      dispatchInputEvent(element, text);
      return true;
    } catch (_) {
      return false;
    }
  }

  function nativeAppend(element, text) {
    if (!element) return false;
    const current = 'value' in element ? element.value : element.textContent;
    return nativeSet(element, current ? `${current}\n${text}` : text);
  }

  function placeCaretAtEnd(element) {
    const selection = window.getSelection();
    if (!selection) return;
    const range = document.createRange();
    range.selectNodeContents(element);
    range.collapse(false);
    selection.removeAllRanges();
    selection.addRange(range);
  }

  function richSet(element, text) {
    if (!element) return false;
    try {
      element.focus({ preventScroll: true });
      const selection = window.getSelection();
      if (selection) {
        const range = document.createRange();
        range.selectNodeContents(element);
        selection.removeAllRanges();
        selection.addRange(range);
      }
      const inserted = document.execCommand('insertText', false, text);
      if (!inserted) {
        element.textContent = text;
        dispatchInputEvent(element, text);
      }
      placeCaretAtEnd(element);
      return true;
    } catch (_) {
      try {
        element.textContent = text;
        placeCaretAtEnd(element);
        dispatchInputEvent(element, text);
        return true;
      } catch (_) {
        return false;
      }
    }
  }

  function richAppend(element, text) {
    if (!element) return false;
    try {
      element.focus({ preventScroll: true });
      placeCaretAtEnd(element);
      const current = element.textContent || '';
      const payload = current.trim() ? `\n${text}` : text;
      const inserted = document.execCommand('insertText', false, payload);
      if (!inserted) {
        element.textContent = `${current}${payload}`;
        dispatchInputEvent(element, payload);
      }
      placeCaretAtEnd(element);
      return true;
    } catch (_) {
      return false;
    }
  }

  function quillSet(element, text) {
    if (!element) return false;
    try {
      element.focus({ preventScroll: true });
      const paragraphs = text.split('\n').map(line => line
        ? `<p>${escapeHTML(line)}</p>`
        : '<p><br></p>').join('');
      setHTML(element, paragraphs);
      placeCaretAtEnd(element);
      dispatchInputEvent(element, text);
      return true;
    } catch (_) {
      return false;
    }
  }

  function quillAppend(element, text) {
    return richAppend(element, text);
  }

  function smartSet(element, text) {
    if (!element) return false;
    if (element.classList?.contains('ql-editor')) return quillSet(element, text);
    if (element.isContentEditable) return richSet(element, text);
    return nativeSet(element, text);
  }

  function smartAppend(element, text) {
    if (!element) return false;
    if (element.classList?.contains('ql-editor')) return quillAppend(element, text);
    if (element.isContentEditable) return richAppend(element, text);
    return nativeAppend(element, text);
  }

  const CHATGPT_SEND_SELECTOR = [
    '#composer-submit-button',
    '[data-testid="send-button"]',
    '[data-testid="composer-submit-button"]',
    'button[aria-label="Send prompt"]',
    'button[aria-label="Send message"]',
    'button[aria-label="Send"]',
    'button.composer-submit-btn'
  ].join(', ');

  function rawChatGPTComposerInput() {
    const candidates = document.querySelectorAll(
      '#prompt-textarea[contenteditable="true"][role="textbox"], ' +
      '#prompt-textarea.ProseMirror[contenteditable="true"], ' +
      '[contenteditable="true"][role="textbox"][aria-label="Chat with ChatGPT"]'
    );
    for (const input of candidates) {
      if (!input || !isVisible(input)) continue;
      if (input.closest('[data-testid^="conversation-turn-"], article[data-testid], article')) continue;
      return input;
    }
    return null;
  }

  function chatGPTComposerRoot() {
    const canonical = document.querySelector('form[data-type="unified-composer"]');
    if (canonical && isVisible(canonical)) return canonical;

    const input = rawChatGPTComposerInput();
    if (!input) return null;

    // The current ChatGPT build can render the send control in a sibling shell
    // while the prompt editor remains inside the form. The editor itself is
    // sufficient to identify the bottom composer; send discovery is fenced
    // separately against stable ids/testids and composer proximity.
    const form = input.closest('form');
    if (form && isVisible(form)) return form;

    const shell = input.closest('[data-type="unified-composer"], [data-testid*="composer" i]');
    return shell && isVisible(shell) ? shell : null;
  }

  function isChatGPTComposerInput(element) {
    if (!element || !isVisible(element)) return false;
    const root = chatGPTComposerRoot();
    if (!root || !root.contains(element)) return false;
    if (element.closest('[data-testid^="conversation-turn-"], article[data-testid], article')) return false;

    const isCanonicalId = element.id === 'prompt-textarea';
    const isCanonicalLabel = element.getAttribute('aria-label') === 'Chat with ChatGPT';
    const isEditable = element.isContentEditable || element.tagName === 'TEXTAREA';

    return isEditable && (isCanonicalId || isCanonicalLabel);
  }

  function getChatGPTInput() {
    const root = chatGPTComposerRoot();
    if (!root) return null;

    const candidates = [
      root.querySelector('#prompt-textarea[contenteditable="true"][role="textbox"]'),
      root.querySelector('#prompt-textarea.ProseMirror[contenteditable="true"]'),
      root.querySelector('[contenteditable="true"][role="textbox"][aria-label="Chat with ChatGPT"]')
    ];

    for (const candidate of candidates) {
      if (candidate && isChatGPTComposerInput(candidate)) return candidate;
    }
    return null;
  }

  function chatGPTSendNearComposer(element, root = chatGPTComposerRoot()) {
    if (!element || !isVisible(element)) return false;
    if (element.closest('[data-testid^="conversation-turn-"], article[data-testid], article')) return false;
    if (root?.contains(element)) return true;

    // Stable ChatGPT ids/testids are globally unique enough to survive builds
    // where the button is rendered just outside the <form>.
    if (element.matches('#composer-submit-button, [data-testid="send-button"], [data-testid="composer-submit-button"]')) {
      return true;
    }

    const input = rawChatGPTComposerInput();
    if (!input) return false;
    const inputForm = input.closest('form');
    const buttonForm = element.closest('form');
    if (inputForm && buttonForm && inputForm === buttonForm) return true;

    // Locale/A-B fallbacks without a stable id are accepted only when they sit
    // in the same nearby composer shell, never merely because the page has a
    // random "Send" button elsewhere.
    let ancestor = input.parentElement;
    for (let depth = 0; ancestor && depth < 7; depth += 1, ancestor = ancestor.parentElement) {
      if (ancestor === document.body || ancestor === document.documentElement) break;
      if (ancestor.contains(element)) return true;
    }
    return false;
  }

  function isChatGPTSend(element) {
    if (!element || !isVisible(element)) return false;
    if (!element.matches(CHATGPT_SEND_SELECTOR) && element.getAttribute('type') !== 'submit') return false;
    return chatGPTSendNearComposer(element);
  }

  function getChatGPTSend() {
    const root = chatGPTComposerRoot();

    // First use the live composer subtree when possible.
    if (root) {
      for (const candidate of root.querySelectorAll(CHATGPT_SEND_SELECTOR)) {
        if (isChatGPTSend(candidate)) return candidate;
      }
    }

    // Then allow the stable ChatGPT id/testid to live just outside the form.
    // This is a real current-A/B DOM shape and was the reason START could
    // prepare the audit perfectly while never finding the visibly enabled arrow.
    for (const candidate of document.querySelectorAll(
      '#composer-submit-button, [data-testid="send-button"], [data-testid="composer-submit-button"]'
    )) {
      if (isChatGPTSend(candidate)) return candidate;
    }

    // Exact aria-label fallbacks are proximity-fenced to the live composer.
    for (const candidate of document.querySelectorAll(
      'button[aria-label="Send prompt"], button[aria-label="Send message"], button[aria-label="Send"]'
    )) {
      if (isChatGPTSend(candidate)) return candidate;
    }

    // Last-resort submit discovery: allow either the root or the editor's form,
    // but still reject non-send semantic controls.
    const submitScope = root || rawChatGPTComposerInput()?.closest('form');
    if (!submitScope) return null;
    const submitCandidates = Array.from(submitScope.querySelectorAll('button[type="submit"]')).filter(candidate => {
      if (!candidate || !isVisible(candidate)) return false;
      if (candidate.closest('[data-testid^="conversation-turn-"], article[data-testid], article')) return false;
      const semantic = `${candidate.id || ''} ${candidate.getAttribute('aria-label') || ''} ${candidate.getAttribute('data-testid') || ''} ${candidate.className || ''}`.toLowerCase();
      if (/(voice|dictat|microphone|record|stop|cancel|retry|attach|upload|tool)/i.test(semantic)) return false;
      return chatGPTSendNearComposer(candidate, root);
    });
    return submitCandidates.length === 1 ? submitCandidates[0] : null;
  }


  function chatGPTUploadInput() {
    const root = chatGPTComposerRoot();
    if (!root || root.hasAttribute('inert')) return null;
    const input = root.querySelector('#upload-files[type="file"], input[type="file"][multiple]');
    if (!input || input.disabled) return null;
    return input;
  }

  function chatGPTComposerAttachmentTiles(root = chatGPTComposerRoot()) {
    if (!root) return [];
    return Array.from(root.querySelectorAll('[role="group"][aria-label]')).filter(tile => {
      return Boolean(tile.querySelector('button[aria-label^="Remove file"], button[name="expand-file-tile"]'));
    });
  }

  function chatGPTReadyComposerAttachments(root = chatGPTComposerRoot()) {
    if (!root || root.hasAttribute('inert')) return [];

    return chatGPTComposerAttachmentTiles(root).filter(tile => {
      if (!tile?.isConnected || !isVisible(tile) || chatGPTAttachmentIsBusy(tile)) return false;

      const label = String(tile.getAttribute('aria-label') || '').trim();
      if (!label) return false;

      // Exclude AI ChatButtons' own generated command attachment if it is
      // briefly still visible around Send. START must represent the user's
      // project/archive/file, not our AUDIT_CORE_*.md instruction file.
      if (/^AUDIT_(?:CORE|SECOND_WAVE|PERFORMANCE)_[A-Za-z0-9_-]+\.md$/i.test(label)) {
        return false;
      }
      return true;
    });
  }

  function acbElementFromNode(node) {
    if (!node) return null;
    if (node.nodeType === 1) return node;
    return node.parentElement || null;
  }

  function mutationTargetsOwnWidget(mutation) {
    const target = acbElementFromNode(mutation?.target);
    return Boolean(panel && target && (target === panel || panel.contains(target)));
  }

  function externalMutationRecords(records) {
    return Array.from(records || []).filter(record => !mutationTargetsOwnWidget(record));
  }

  function mutationTouchesNode(record, root) {
    if (!record || !root) return false;

    const target = acbElementFromNode(record.target);
    if (target && (target === root || root.contains(target) || target.contains?.(root))) {
      return true;
    }

    for (const node of [...Array.from(record.addedNodes || []), ...Array.from(record.removedNodes || [])]) {
      const element = acbElementFromNode(node);
      if (!element) continue;
      if (element === root || root.contains(element) || element.contains?.(root)) return true;
    }

    return false;
  }

  function composerAttachmentSignature(root = chatGPTComposerRoot()) {
    if (!root || root.hasAttribute('inert')) return 'composer:none';

    const tiles = chatGPTComposerAttachmentTiles(root);
    if (!tiles.length) return 'composer:0';

    return `composer:${tiles.map(tile => {
      const name = String(tile.getAttribute('aria-label') || '').trim().toLowerCase();
      const busy = chatGPTAttachmentIsBusy(tile) ? '1' : '0';
      return `${name}|${busy}`;
    }).sort().join('||')}`;
  }

  function scheduleMiniAttachmentRefresh(records = []) {
    if (
      !state?.superCompact ||
      detectSite().key !== 'chatgpt' ||
      autoRuntime?.stage !== 'idle'
    ) return;

    const external = externalMutationRecords(records);
    if (!external.length) return;

    const composer = chatGPTComposerRoot();
    if (!composer) {
      // Composer replacement can temporarily produce a null root. Only repaint
      // when that changes the cached signature, and do it once after the batch.
      if (miniAttachmentSignature !== 'composer:none' && !miniAttachmentRefreshTimer) {
        miniAttachmentRefreshTimer = setTimeout(() => {
          miniAttachmentRefreshTimer = 0;
          const signature = composerAttachmentSignature();
          if (signature === miniAttachmentSignature) return;
          miniAttachmentSignature = signature;
          reconcileProjectIdentityFromComposer({ rename: true });
          renderAutoAuditState();
        }, 80);
      }
      return;
    }

    if (!external.some(record => mutationTouchesNode(record, composer))) return;
    if (miniAttachmentRefreshTimer) return;

    miniAttachmentRefreshTimer = setTimeout(() => {
      miniAttachmentRefreshTimer = 0;
      const signature = composerAttachmentSignature();
      if (signature === miniAttachmentSignature) return;
      miniAttachmentSignature = signature;
      reconcileProjectIdentityFromComposer({ rename: true });
      renderAutoAuditState();
    }, 80);
  }

  function chatGPTReadyAttachmentSummary() {
    if (detectSite().key !== 'chatgpt') {
      return { ready: false, count: 0, names: [], reason: 'ChatGPT only.' };
    }

    const root = chatGPTComposerRoot();
    if (!root || root.hasAttribute('inert')) {
      return { ready: false, count: 0, names: [], reason: 'Main ChatGPT composer is not available.' };
    }

    const allTiles = chatGPTComposerAttachmentTiles(root);
    const readyTiles = chatGPTReadyComposerAttachments(root);
    const names = readyTiles
      .map(tile => String(tile.getAttribute('aria-label') || '').trim())
      .filter(Boolean);

    if (readyTiles.length) {
      return {
        ready: true,
        count: readyTiles.length,
        names,
        reason: `${readyTiles.length} project attachment${readyTiles.length === 1 ? '' : 's'} ready.`
      };
    }

    if (allTiles.some(tile => chatGPTAttachmentIsBusy(tile))) {
      return {
        ready: false,
        count: 0,
        names: [],
        reason: 'Attachment is still registering with ChatGPT.'
      };
    }

    return {
      ready: false,
      count: 0,
      names: [],
      reason: 'Attach a project/archive/file to enable START AUDITING.'
    };
  }

  function miniStartAuditState() {
    if (!state?.superCompact || detectSite().key !== 'chatgpt') {
      return { available: false, busy: false, attachment: null, retryPrepared: false };
    }

    const prepared = readStartAuditHandoff();
    if (startHandoffIsPrepared(prepared) && startHandoffComposerStillPrepared(prepared)) {
      return {
        available: !auditStartInFlight && !actionInFlight && !chatGPTIsGenerating(),
        busy: auditStartInFlight || actionInFlight,
        attachment: chatGPTReadyAttachmentSummary(),
        retryPrepared: true
      };
    }

    if (autoRuntime && autoRuntime.stage !== 'idle') {
      return { available: false, busy: false, attachment: null, retryPrepared: false };
    }
    if (auditStartInFlight || actionInFlight) {
      return { available: false, busy: true, attachment: chatGPTReadyAttachmentSummary(), retryPrepared: false };
    }
    if (chatGPTIsGenerating()) return { available: false, busy: false, attachment: null, retryPrepared: false };

    const attachment = chatGPTReadyAttachmentSummary();
    return { available: Boolean(attachment.ready), busy: false, attachment, retryPrepared: false };
  }

  function chatGPTFindComposerAttachment(filename, root = chatGPTComposerRoot()) {
    const wanted = String(filename || '').trim().toLowerCase();
    if (!wanted) return null;
    return chatGPTComposerAttachmentTiles(root).find(tile => {
      return String(tile.getAttribute('aria-label') || '').trim().toLowerCase() === wanted;
    }) || null;
  }

  function chatGPTAttachmentIsBusy(tile) {
    if (!tile) return false;
    return Array.from(tile.querySelectorAll('[class*="animate-spin"]')).some(node => isVisible(node));
  }

  function waitForDomCondition(root, predicate, timeoutMs) {
    return new Promise(resolve => {
      let settled = false;
      let observer = null;
      let timer = null;

      const finish = value => {
        if (settled) return;
        settled = true;
        if (observer) observer.disconnect();
        if (timer) clearTimeout(timer);
        resolve(value || null);
      };

      const check = () => {
        let value = null;
        try { value = predicate(); } catch (_) { value = null; }
        if (value) finish(value);
      };

      check();
      if (settled) return;

      observer = new MutationObserver(check);
      observer.observe(root, {
        childList: true,
        subtree: true,
        attributes: true,
        attributeFilter: ['disabled', 'aria-disabled', 'class', 'style', 'aria-label']
      });
      check();
      if (settled) return;
      timer = setTimeout(() => finish(null), Math.max(1, timeoutMs));
    });
  }

  function auditKindFromPreset(preset) {
    if (!preset) return '';
    const builtin = String(preset.builtinId || '').toLowerCase();
    if (builtin.includes('audit-core')) return 'core';
    if (builtin.includes('audit-second-wave')) return 'second';
    if (builtin.includes('audit-performance')) return 'performance';
    return classifyAuditMessage(preset.text);
  }

  function safeAttachmentBasename(value) {
    const normalized = String(value || 'COMMAND')
      .normalize('NFKD')
      .replace(/[^a-zA-Z0-9._-]+/g, '_')
      .replace(/^[_\-.]+|[_\-.]+$/g, '')
      .slice(0, 56);
    return normalized || 'COMMAND';
  }

  function promptAttachmentFilename(preset) {
    const kind = auditKindFromPreset(preset);
    const fingerprint = textFingerprint(String(preset?.text || '')).split(':').pop() || 'prompt';
    if (kind && AUDIT_ATTACHMENT_FILES[kind]) {
      const base = AUDIT_ATTACHMENT_FILES[kind].replace(/\.md$/i, '');
      return `${base}_${fingerprint}.md`;
    }
    return `AI_CHATBUTTONS_${safeAttachmentBasename(preset?.name)}_${fingerprint}.md`;
  }

  function promptAttachmentMarker(preset, filename) {
    const kind = auditKindFromPreset(preset);
    const marker = kind === 'core'
      ? 'AUDIT CORE'
      : kind === 'second'
        ? 'AUDIT SECOND WAVE'
        : kind === 'performance'
          ? 'AUDIT PERFORMANCE / STABILITY / EFFECTIVENESS'
          : `COMMAND: ${String(preset?.name || 'Attached prompt')}`;

    const lines = [
      marker,
      `The complete command is attached as "${filename}".`,
      'Treat that attached file as my full instruction for this turn and execute it exactly; do not merely summarize the file.'
    ];

    if (preset?.machineReceipt) {
      lines.push(`${AUTO_SEND_RECEIPT_PREFIX}: ${preset.machineReceipt}`);
    }

    return lines.join('\n');
  }

  function shouldUseChatGPTPromptAttachment(site, preset) {
    if (site?.key !== 'chatgpt' || !preset?.text) return false;
    if (preset.forceTextDelivery === true) return false;
    const mode = CHATGPT_PROMPT_DELIVERY_MODES.includes(state?.chatgptPromptDelivery)
      ? state.chatgptPromptDelivery
      : 'auto';
    if (mode === 'text') return false;
    if (mode === 'file') return true;
    return String(preset.text).length >= CHATGPT_LONG_PROMPT_THRESHOLD;
  }

  function setNativeFileList(input, files) {
    if (!input || typeof DataTransfer !== 'function') return false;
    try {
      const transfer = new DataTransfer();
      for (const file of files) transfer.items.add(file);
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'files')?.set;
      if (setter) setter.call(input, transfer.files);
      else input.files = transfer.files;
      input.dispatchEvent(new Event('change', { bubbles: true, cancelable: false }));
      return true;
    } catch (_) {
      return false;
    }
  }

  async function attachChatGPTPromptFile(preset, input, options = {}) {
    const root = chatGPTComposerRoot();
    if (!root || !input || !root.contains(input) || root.hasAttribute('inert')) {
      return { ok: false, reason: 'composer-unavailable' };
    }

    const filename = promptAttachmentFilename(preset);
    let tile = chatGPTFindComposerAttachment(filename, root);

    if (!tile) {
      const upload = chatGPTUploadInput();
      if (!upload || !root.contains(upload)) {
        return { ok: false, reason: 'upload-input-unavailable', filename };
      }
      if (typeof File !== 'function' || typeof DataTransfer !== 'function') {
        return { ok: false, reason: 'file-api-unavailable', filename };
      }

      const file = new File([String(preset.text || '')], filename, {
        type: 'text/markdown;charset=utf-8',
        lastModified: Date.now()
      });

      if (!setNativeFileList(upload, [file])) {
        return { ok: false, reason: 'file-injection-rejected', filename };
      }

      tile = await waitForDomCondition(root, () => {
        const candidate = chatGPTFindComposerAttachment(filename, root);
        return candidate && !chatGPTAttachmentIsBusy(candidate) ? candidate : null;
      }, CHATGPT_ATTACHMENT_TIMEOUT_MS);

      if (!tile) {
        return { ok: false, reason: 'attachment-timeout', filename };
      }
    } else if (chatGPTAttachmentIsBusy(tile)) {
      tile = await waitForDomCondition(root, () => {
        const candidate = chatGPTFindComposerAttachment(filename, root);
        return candidate && !chatGPTAttachmentIsBusy(candidate) ? candidate : null;
      }, CHATGPT_ATTACHMENT_TIMEOUT_MS);
      if (!tile) return { ok: false, reason: 'attachment-timeout', filename };
    }

    const marker = promptAttachmentMarker(preset, filename);
    const current = composerPlainText(input);
    if (options.canonicalComposerOnly && cleanTurnText(current)) {
      return { ok: false, reason: 'composer-changed-before-marker', filename };
    }
    if (!current.includes(filename)) {
      if (!smartAppend(input, marker)) {
        return { ok: false, reason: 'marker-write-rejected', filename };
      }
    }

    return { ok: true, filename, marker, tile };
  }

  async function waitForChatGPTSendReady(timeoutMs = CHATGPT_ATTACHMENT_TIMEOUT_MS) {
    // Do not observe one captured composer root for the whole attachment wait.
    // ChatGPT can replace the unified-composer subtree while an injected file is
    // being registered. The old root then receives no more mutations, leaving a
    // visibly enabled Send button in the NEW root while this function sleeps all
    // the way to timeout. Re-resolve the live composer/button on every probe.
    const deadline = Date.now() + Math.max(1, Number(timeoutMs) || CHATGPT_ATTACHMENT_TIMEOUT_MS);
    while (Date.now() < deadline) {
      const button = getChatGPTSend();
      if (
        button &&
        button.isConnected &&
        isVisible(button) &&
        !button.disabled &&
        button.getAttribute('aria-disabled') !== 'true'
      ) return button;

      await sleep(Math.min(120, Math.max(20, deadline - Date.now())));
    }

    const button = getChatGPTSend();
    return button && button.isConnected && isVisible(button) &&
      !button.disabled && button.getAttribute('aria-disabled') !== 'true'
      ? button
      : null;
  }

  const SITES = {
    chatgpt: {
      hosts: ['chat.openai.com', 'chatgpt.com'],
      label: 'ChatGPT',
      getInput: getChatGPTInput,
      getSend: getChatGPTSend,
      validateInput: isChatGPTComposerInput,
      validateSend: isChatGPTSend,
      allowEnterFallback: false
    },
    claude: {
      hosts: ['claude.ai'],
      label: 'Claude',
      getInput: () => queryDeepFirst('.ProseMirror[contenteditable="true"], div[contenteditable="true"][role="textbox"], div[contenteditable="true"]'),
      getSend: () => queryDeepFirst('button[aria-label="Send Message"], button[aria-label*="Send" i], button[type="submit"]')
    },
    deepseek: {
      hosts: ['chat.deepseek.com'],
      label: 'DeepSeek',
      getInput: () => queryDeepFirst('#chat-input, textarea[placeholder], textarea'),
      getSend: () => queryDeepFirst('[aria-label="send" i], button[type="submit"]')
    },
    qwen: {
      hosts: ['chat.qwen.ai', 'qwen.ai', 'tongyi.aliyun.com'],
      label: 'Qwen',
      getInput: () => queryDeepFirst('textarea.message-input-textarea, textarea[placeholder*="Message" i], textarea[placeholder], textarea, div[contenteditable="true"][role="textbox"]'),
      getSend: () => queryDeepFirst('[class*="message-input-right-button-send"] button, button[type="submit"], button[aria-label*="send" i]')
    },
    grok: {
      hosts: ['grok.com', 'x.com'],
      label: 'Grok',
      pageMatch: ({ host, pathname }) => host.endsWith('grok.com') || (host.endsWith('x.com') && pathname.startsWith('/i/grok')),
      getInput: () => queryDeepFirst('div.tiptap.ProseMirror[contenteditable="true"], div[contenteditable="true"][role="textbox"], div[contenteditable="true"]'),
      getSend: () => queryDeepFirst('button[type="submit"], button[aria-label*="send" i]')
    },
    gemini: {
      hosts: ['gemini.google.com'],
      label: 'Gemini',
      getInput: () => queryDeepFirst('.ql-editor, [data-test-id="rich-textarea-input"], div[contenteditable="true"][aria-label*="Message" i], div[contenteditable="true"][role="textbox"], textarea[placeholder*="Ask" i], textarea'),
      getSend: () => queryDeepFirst('button[aria-label*="Send message" i], button[aria-label*="Send" i], [data-test-id="send-button-container"] button, .send-button, button[type="submit"]')
    },
    copilot: {
      hosts: ['copilot.microsoft.com', 'www.bing.com'],
      label: 'Copilot',
      pageMatch: ({ host, pathname }) => host.endsWith('copilot.microsoft.com') || (host.endsWith('bing.com') && /^\/chat(?:\/|$)/i.test(pathname)),
      getInput: () => queryDeepFirst('textarea[data-testid="composer-input"], textarea#searchbox, textarea[placeholder], textarea, [contenteditable="true"][role="textbox"], [contenteditable="true"]'),
      getSend: () => queryDeepFirst('button[type="submit"], button[aria-label*="send" i]')
    },
    kimi: {
      hosts: ['kimi.moonshot.cn', 'kimi.com'],
      label: 'Kimi',
      getInput: () => queryDeepFirst('.ProseMirror[contenteditable="true"], div[contenteditable="true"][data-placeholder], div[contenteditable="true"][role="textbox"], div[contenteditable="true"]'),
      getSend: () => queryDeepFirst('button[aria-label*="send" i], [data-testid*="send" i], button[type="submit"]')
    },
    duck: {
      hosts: ['duck.ai', 'duckduckgo.com'],
      label: 'Duck AI',
      pageMatch: ({ host, pathname, searchParams }) => {
        if (host.endsWith('duck.ai')) return true;
        if (!host.endsWith('duckduckgo.com')) return false;
        return /^\/(?:chat|duckai)(?:\/|$)/i.test(pathname) ||
          /^(?:chat|duckai)$/i.test(String(searchParams.get('ia') || '')) ||
          /^(?:chat|duckai)$/i.test(String(searchParams.get('iax') || ''));
      },
      getInput: () => queryDeepFirst('textarea[placeholder], textarea, input[type="text"]'),
      getSend: () => queryDeepFirst('button[aria-label*="send" i], button[type="submit"]')
    },
    mistral: {
      hosts: ['chat.mistral.ai'],
      label: 'Mistral',
      getInput: () => queryDeepFirst('textarea[placeholder], textarea, [contenteditable="true"]'),
      getSend: () => queryDeepFirst('button[aria-label*="send" i], button[type="submit"]')
    },
    huggingface: {
      hosts: ['huggingface.co'],
      label: 'HuggingChat',
      pageMatch: ({ pathname }) => /^\/chat(?:\/|$)/i.test(pathname),
      getInput: () => queryDeepFirst('textarea[placeholder], textarea, [contenteditable="true"]'),
      getSend: () => queryDeepFirst('button[type="submit"], button[aria-label*="Send" i]')
    },
    perplexity: {
      hosts: ['perplexity.ai'],
      label: 'Perplexity',
      getInput: () => queryDeepFirst('textarea[placeholder], textarea, [contenteditable="true"]'),
      getSend: () => queryDeepFirst('button[aria-label*="Submit" i], button[type="submit"], button[aria-label*="send" i]')
    },
    poe: {
      hosts: ['poe.com'],
      label: 'Poe',
      getInput: () => queryDeepFirst('textarea[placeholder], textarea, [contenteditable="true"]'),
      getSend: () => queryDeepFirst('button[aria-label*="send" i], button[type="submit"]')
    },
    pi: {
      hosts: ['pi.ai'],
      label: 'Pi',
      getInput: () => queryDeepFirst('textarea[placeholder], textarea, [contenteditable="true"]'),
      getSend: () => queryDeepFirst('button[aria-label*="send" i], button[type="submit"]')
    },
    phind: {
      hosts: ['phind.com'],
      label: 'Phind',
      getInput: () => queryDeepFirst('textarea[placeholder], textarea, [contenteditable="true"]'),
      getSend: () => queryDeepFirst('button[aria-label*="send" i], button[type="submit"]')
    },
    you: {
      hosts: ['you.com'],
      label: 'You.com',
      getInput: () => queryDeepFirst('textarea[placeholder], textarea, [contenteditable="true"]'),
      getSend: () => queryDeepFirst('button[aria-label*="send" i], button[type="submit"]')
    }
  };

  function detectSite() {
    const host = location.hostname.replace(/^www\./, '').toLowerCase();
    const pathname = location.pathname || '/';
    const searchParams = new URLSearchParams(location.search || '');

    for (const [key, site] of Object.entries(SITES)) {
      const hostMatches = site.hosts.some(candidate => {
        const normalized = candidate.replace(/^www\./, '').toLowerCase();
        return host === normalized || host.endsWith(`.${normalized}`);
      });
      if (!hostMatches) continue;

      if (site.pageMatch && !site.pageMatch({ host, pathname, searchParams })) continue;
      return { key, ...site };
    }

    // Unsupported/non-chat surfaces are intentionally inert. A universal generic
    // textarea fallback can target search boxes or edit fields on unrelated pages.
    return {
      key: 'unknown',
      label: host || 'Unsupported page',
      getInput: () => null,
      getSend: () => null,
      allowEnterFallback: false
    };
  }

  function resetElementCache(siteKey = '') {
    elementCache.siteKey = siteKey;
    elementCache.input = null;
    elementCache.send = null;
  }

  function cachedSiteElement(site, kind) {
    if (!site || (kind !== 'input' && kind !== 'send')) return null;
    if (elementCache.siteKey !== site.key) resetElementCache(site.key);

    const validator = kind === 'input' ? site.validateInput : site.validateSend;
    const cached = elementCache[kind];
    if (cached && isVisible(cached) && (!validator || validator(cached))) return cached;

    elementCache[kind] = null;
    const found = kind === 'input' ? site.getInput() : site.getSend();
    if (!found || !isVisible(found) || (validator && !validator(found))) return null;

    elementCache[kind] = found;
    return found;
  }

  function yieldToBrowser() {
    return new Promise(resolve => {
      if (typeof requestAnimationFrame === 'function') requestAnimationFrame(() => resolve());
      else setTimeout(resolve, 0);
    });
  }

  function makeAuditCategory() {
    return {
      id: uid(),
      name: 'Audit',
      presets: BUILTIN_PRESETS.map(preset => ({
        id: uid(),
        builtinId: preset.builtinId,
        name: preset.name,
        desc: preset.desc,
        text: preset.text
      }))
    };
  }

  function defaultState() {
    const audit = makeAuditCategory();
    return {
      stateVersion: STATE_VERSION,
      builtinRevision: BUILTIN_REVISION,
      builtinsSeededV2: true,
      popupPos: { x: 16, y: 72 },
      posLocked: false,
      collapsed: false,
      superCompact: false,
      opacity: 100,
      panelSize: 'normal',
      autoAuditEnabled: false,
      autoAuditStrictGate: true,
      autoAuditDelayMs: 1200,
      autoAuditTimeoutMin: 180,
      chatgptPromptDelivery: 'auto',
      autoRenameChat: true,
      autoSaveAuditFiles: true,
      bridgeEnabled: true,
      bridgeUrl: BRIDGE_DEFAULT_URL,
      activeCategoryId: audit.id,
      categories: [audit]
    };
  }

  function canonicalBuiltinId(value) {
    const id = String(value || '');
    if (!id) return '';
    for (const builtin of BUILTIN_PRESETS) {
      if (id === builtin.builtinId || builtin.legacyIds.includes(id)) return builtin.builtinId;
    }
    return '';
  }

  function uniquePresetName(category, baseName, exceptId = '') {
    const used = new Set(
      category.presets
        .filter(preset => preset.id !== exceptId)
        .map(preset => String(preset.name || '').toLowerCase())
    );
    if (!used.has(baseName.toLowerCase())) return baseName;
    for (let index = 1; index < 1000; index += 1) {
      const suffix = index === 1 ? ' (custom)' : ` (custom ${index})`;
      const candidate = `${baseName}${suffix}`.slice(0, 40);
      if (!used.has(candidate.toLowerCase())) return candidate;
    }
    return `${baseName.slice(0, 30)} ${uid().slice(-6)}`.slice(0, 40);
  }

  function sanitizeCategories(categories) {
    if (!Array.isArray(categories)) return [];
    const clean = [];
    for (const rawCategory of categories) {
      if (!rawCategory || typeof rawCategory !== 'object') continue;
      const name = String(rawCategory.name || '').trim().slice(0, 30);
      if (!name) continue;
      const presets = [];
      if (Array.isArray(rawCategory.presets)) {
        for (const rawPreset of rawCategory.presets) {
          if (!rawPreset || typeof rawPreset !== 'object') continue;
          const presetName = String(rawPreset.name || '').trim().slice(0, 40);
          const text = String(rawPreset.text || '').trim();
          if (!presetName || !text) continue;
          const builtinId = canonicalBuiltinId(rawPreset.builtinId);
          presets.push({
            id: String(rawPreset.id || uid()),
            ...(builtinId ? { builtinId } : {}),
            name: presetName,
            desc: String(rawPreset.desc || '').trim().slice(0, 100),
            text
          });
        }
      }
      clean.push({
        id: String(rawCategory.id || uid()),
        name,
        presets
      });
    }
    return clean;
  }

  function syncBuiltins(data) {
    let audit = data.categories.find(category => category.name.toLowerCase() === 'audit');
    if (!audit) {
      if (data.categories.length < MAX_CATEGORIES) {
        audit = { id: uid(), name: 'Audit', presets: [] };
        data.categories.unshift(audit);
      } else {
        audit = data.categories[0];
      }
    }

    for (const builtin of BUILTIN_PRESETS) {
      const matches = [];
      for (const category of data.categories) {
        for (const preset of category.presets) {
          if (canonicalBuiltinId(preset.builtinId) === builtin.builtinId) {
            matches.push({ category, preset });
          }
        }
      }

      let existing = matches[0]?.preset || null;
      if (existing) {
        existing.builtinId = builtin.builtinId;
        existing.name = builtin.name;
        existing.desc = builtin.desc;
        existing.text = builtin.text;

        // Duplicate legacy identities are downgraded to custom entries rather than
        // being allowed to impersonate the same canonical built-in twice.
        for (const duplicate of matches.slice(1)) {
          delete duplicate.preset.builtinId;
          duplicate.preset.name = uniquePresetName(duplicate.category, `${builtin.name} (custom)`, duplicate.preset.id);
        }
        continue;
      }

      // A same-name custom preset is not canonical identity. Keep it, but make the
      // distinction visible if it occupies the built-in category/name slot.
      const nameCollision = audit.presets.find(preset =>
        !preset.builtinId && preset.name.toLowerCase() === builtin.name.toLowerCase()
      );
      if (nameCollision) {
        nameCollision.name = uniquePresetName(audit, `${builtin.name} (custom)`, nameCollision.id);
      }

      if (audit.presets.length < MAX_PRESETS) {
        audit.presets.push({
          id: uid(),
          builtinId: builtin.builtinId,
          name: builtin.name,
          desc: builtin.desc,
          text: builtin.text
        });
      }
    }

    const allCanonical = BUILTIN_PRESETS.every(builtin => {
      const matches = data.categories.flatMap(category =>
        category.presets.filter(preset => preset.builtinId === builtin.builtinId)
      );
      return matches.length === 1 &&
        matches[0].name === builtin.name &&
        matches[0].desc === builtin.desc &&
        matches[0].text === builtin.text;
    });

    if (allCanonical) {
      data.builtinRevision = BUILTIN_REVISION;
      data.builtinsSeededV2 = true;
      return true;
    }

    // Never claim a revision that was not actually reconciled.
    data.builtinRevision = Math.min(Number(data.builtinRevision) || 0, BUILTIN_REVISION - 1);
    return false;
  }

  function loadState() {
    let data = null;
    try {
      const raw = GM_getValue(STORAGE_KEY, null);
      if (raw) data = JSON.parse(raw);
    } catch (_) { }

    if (!data || typeof data !== 'object') return defaultState();

    if (Array.isArray(data.presets) && !Array.isArray(data.categories)) {
      data.categories = [{ id: uid(), name: 'General', presets: data.presets }];
      delete data.presets;
    }

    const categories = sanitizeCategories(data.categories);
    if (!categories.length) return defaultState();

    const clean = {
      stateVersion: STATE_VERSION,
      builtinRevision: Number(data.builtinRevision) || 0,
      builtinsSeededV2: Boolean(data.builtinsSeededV2),
      popupPos: data.popupPos && Number.isFinite(Number(data.popupPos.x)) && Number.isFinite(Number(data.popupPos.y))
        ? { x: Number(data.popupPos.x), y: Number(data.popupPos.y) }
        : { x: 16, y: 72 },
      posLocked: Boolean(data.posLocked),
      collapsed: Boolean(data.collapsed),
      superCompact: Boolean(data.superCompact),
      opacity: OPACITY_LEVELS.includes(Number(data.opacity)) ? Number(data.opacity) : 100,
      panelSize: Object.prototype.hasOwnProperty.call(PANEL_SIZES, String(data.panelSize || ''))
        ? String(data.panelSize)
        : 'normal',
      autoAuditEnabled: false, // legacy field retained for compatibility; per-chat runtime owns enablement
      autoAuditStrictGate: data.autoAuditStrictGate !== false,
      autoAuditDelayMs: AUTO_DELAYS_MS.includes(Number(data.autoAuditDelayMs))
        ? Number(data.autoAuditDelayMs)
        : 1200,
      autoAuditTimeoutMin: AUTO_STAGE_TIMEOUTS.includes(Number(data.autoAuditTimeoutMin))
        ? Number(data.autoAuditTimeoutMin)
        : 180,
      chatgptPromptDelivery: CHATGPT_PROMPT_DELIVERY_MODES.includes(String(data.chatgptPromptDelivery || ''))
        ? String(data.chatgptPromptDelivery)
        : 'auto',
      autoRenameChat: data.autoRenameChat !== false,
      autoSaveAuditFiles: true, // invariant: every COMPLETE audit is always queued for persistence
      bridgeEnabled: data.bridgeEnabled !== false,
      bridgeUrl: String(data.bridgeUrl || BRIDGE_DEFAULT_URL),
      activeCategoryId: String(data.activeCategoryId || ''),
      categories
    };

    syncBuiltins(clean);

    if (!clean.activeCategoryId || !clean.categories.some(category => category.id === clean.activeCategoryId)) {
      clean.activeCategoryId = clean.categories[0].id;
    }

    return clean;
  }

  function saveState() {
    try {
      const payload = JSON.stringify(state);
      GM_setValue(STORAGE_KEY, payload);
      const verified = GM_getValue(STORAGE_KEY, null);
      if (verified !== payload) throw new Error('userscript storage read-back mismatch');
      return true;
    } catch (error) {
      setStatus(
        `Could not save settings: ${error?.message || 'userscript storage rejected the write'}. The change was rolled back.`,
        'error'
      );
      return false;
    }
  }

  function snapshotState() {
    return JSON.stringify(state);
  }

  function restoreStateSnapshot(snapshot) {
    try {
      state = JSON.parse(snapshot);
      return true;
    } catch (_) {
      return false;
    }
  }

  function renderStateViewsAfterMutation() {
    if (!panel) return;
    applyDisplayState();
    updateLockState();
    renderCategoryTabs();
    renderCommands();
    renderManageCategory();
    renderManageList();
    renderAutoAuditState();
  }

  function commitStateMutation(mutator, failureMessage = 'The change could not be persisted and was rolled back.') {
    const before = snapshotState();
    try {
      mutator();
    } catch (error) {
      restoreStateSnapshot(before);
      renderStateViewsAfterMutation();
      setStatus(`Change failed before save: ${error?.message || 'unexpected mutation error'}.`, 'error');
      return false;
    }

    if (saveState()) return true;

    restoreStateSnapshot(before);
    renderStateViewsAfterMutation();
    setStatus(failureMessage, 'error');
    return false;
  }

  function activeCategory() {
    return state.categories.find(category => category.id === state.activeCategoryId) || state.categories[0] || null;
  }

  function setStatus(message, kind = 'info') {
    if (!panel) return;
    const status = panel.querySelector('#acb-status-text');
    if (!status) return;
    status.textContent = message;
    status.dataset.kind = kind;
    status.title = message;
  }

  function viewportRect() {
    const root = document.documentElement;
    const visual = window.visualViewport;

    const width = Math.max(
      1,
      Number(visual?.width) || Number(window.innerWidth) || Number(root?.clientWidth) || 1
    );
    const height = Math.max(
      1,
      Number(visual?.height) || Number(window.innerHeight) || Number(root?.clientHeight) || 1
    );
    const left = Number.isFinite(Number(visual?.offsetLeft)) ? Number(visual.offsetLeft) : 0;
    const top = Number.isFinite(Number(visual?.offsetTop)) ? Number(visual.offsetTop) : 0;

    return {
      left,
      top,
      width,
      height,
      right: left + width,
      bottom: top + height
    };
  }

  function currentPanelGeometry(viewport = viewportRect()) {
    const selected = PANEL_SIZES[state.panelSize] || PANEL_SIZES.normal;
    const availableWidth = Math.max(1, viewport.width - (PANEL_EDGE_MARGIN * 2));
    const availableHeight = Math.max(1, viewport.height - (PANEL_EDGE_MARGIN * 2));
    const normalWidth = Math.max(1, Math.min(selected.width, availableWidth));
    const expandedHeight = Math.max(1, Math.min(selected.height, availableHeight));
    const superCompact = Boolean(state.superCompact);

    return {
      viewport,
      width: superCompact
        ? Math.max(1, Math.min(SUPER_COMPACT_WIDTH, availableWidth))
        : normalWidth,
      height: superCompact
        ? Math.max(1, Math.min(SUPER_COMPACT_HEIGHT, availableHeight))
        : state.collapsed
          ? Math.min(24, availableHeight)
          : expandedHeight,
      maxWidth: availableWidth,
      maxHeight: availableHeight
    };
  }

  function applyDisplayState(geometry = currentPanelGeometry()) {
    if (!panel) return geometry;

    panel.dataset.collapsed = state.collapsed ? 'true' : 'false';
    panel.dataset.supercompact = state.superCompact ? 'true' : 'false';
    panel.style.setProperty('width', `${geometry.width}px`, 'important');
    panel.style.setProperty('height', `${geometry.height}px`, 'important');
    panel.style.setProperty('max-width', `${geometry.maxWidth}px`, 'important');
    panel.style.setProperty('max-height', `${geometry.maxHeight}px`, 'important');
    panel.style.setProperty('opacity', String(state.opacity / 100), 'important');

    const collapse = panel.querySelector('#acb-collapse');
    if (collapse) {
      collapse.textContent = state.collapsed ? 'Expand' : 'Collapse';
      collapse.setAttribute('aria-expanded', state.collapsed ? 'false' : 'true');
      collapse.title = state.collapsed ? 'Expand the widget' : 'Collapse the widget to the title bar';
    }

    const superToggle = panel.querySelector('#acb-super-toggle');
    if (superToggle) {
      superToggle.textContent = state.superCompact ? 'Full' : 'Mini';
      superToggle.setAttribute('aria-pressed', state.superCompact ? 'true' : 'false');
      superToggle.title = state.superCompact
        ? 'Restore the full AUDAPACK Widget'
        : 'Switch to the one-line Auto3 monitor';
    }

    const opacity = panel.querySelector('#acb-opacity');
    if (opacity) opacity.value = String(state.opacity);

    const size = panel.querySelector('#acb-size');
    if (size) size.value = state.panelSize;

    return geometry;
  }

  function clampNumber(value, min, max) {
    return Math.max(min, Math.min(value, max));
  }

  function clampPanelPosition(options = {}) {
    if (!panel) return null;

    const commit = options.commit === true;
    const report = options.report === true;
    const geometry = applyDisplayState(currentPanelGeometry());
    const viewport = geometry.viewport;
    const rect = panel.getBoundingClientRect();

    // Use a smaller edge margin only when the viewport itself is extremely tiny.
    const marginX = Math.min(PANEL_EDGE_MARGIN, Math.max(0, (viewport.width - rect.width) / 2));
    const marginY = Math.min(PANEL_EDGE_MARGIN, Math.max(0, (viewport.height - rect.height) / 2));
    const minX = viewport.left + marginX;
    const minY = viewport.top + marginY;
    const maxX = Math.max(minX, viewport.right - rect.width - marginX);
    const maxY = Math.max(minY, viewport.bottom - rect.height - marginY);

    const rawX = Number(state.popupPos?.x);
    const rawY = Number(state.popupPos?.y);
    const desiredX = Number.isFinite(rawX) ? rawX : minX;
    const desiredY = Number.isFinite(rawY) ? rawY : minY;
    const x = clampNumber(desiredX, minX, maxX);
    const y = clampNumber(desiredY, minY, maxY);
    const corrected = Math.abs(x - desiredX) > 0.5 || Math.abs(y - desiredY) > 0.5;

    // Inline !important makes the userscript the single owner of its coordinates.
    panel.style.setProperty('left', `${x}px`, 'important');
    panel.style.setProperty('top', `${y}px`, 'important');

    if (commit) {
      state.popupPos = { x, y };
    } else if (report && corrected) {
      setStatus('Panel was kept inside the current viewport after a window, zoom, or screen change. Your saved position was preserved.', 'info');
    }

    return { x, y, corrected, viewport, width: rect.width, height: rect.height };
  }

  function updateLockState() {
    const button = panel?.querySelector('#acb-lock');
    const titlebar = panel?.querySelector('#acb-titlebar');
    if (!button || !titlebar) return;
    button.classList.toggle('acb-active', state.posLocked);
    button.setAttribute('aria-pressed', state.posLocked ? 'true' : 'false');
    titlebar.classList.toggle('acb-movable', !state.posLocked);
  }

  function renderTabs() {
    if (!panel) return;
    for (const button of panel.querySelectorAll('#acb-tabs button')) {
      const selected = button.dataset.view === activeView;
      button.setAttribute('aria-selected', selected ? 'true' : 'false');
    }
    panel.querySelector('#acb-view-commands').hidden = activeView !== 'commands';
    panel.querySelector('#acb-view-manage').hidden = activeView !== 'manage';
    panel.querySelector('#acb-view-settings').hidden = activeView !== 'settings';
  }

  function renderCategoryTabs() {
    const container = panel?.querySelector('#acb-catbar');
    if (!container) return;
    container.textContent = '';
    container.hidden = state.categories.length <= 1;
    for (const category of state.categories) {
      const button = document.createElement('button');
      button.type = 'button';
      button.textContent = category.name;
      button.title = category.name;
      button.setAttribute('role', 'tab');
      button.setAttribute('aria-selected', category.id === state.activeCategoryId ? 'true' : 'false');
      button.addEventListener('click', () => {
        if (!commitStateMutation(
          () => { state.activeCategoryId = category.id; },
          'Category selection could not be persisted; the previous selection was restored.'
        )) return;
        renderCategoryTabs();
        renderCommands();
        renderManageCategory();
        renderManageList();
        hideEditor();
        setStatus(`Selected category: ${category.name}.`, 'info');
      });
      container.appendChild(button);
    }
  }

  function auditWaveForPreset(preset) {
    const builtinId = canonicalBuiltinId(preset?.builtinId);
    if (builtinId.startsWith('audit-core-')) return 'core';
    if (builtinId.startsWith('audit-second-wave-')) return 'second';
    if (builtinId.startsWith('audit-performance-')) return 'performance';
    return '';
  }

  function findAuditPreset(wave) {
    for (const category of state.categories) {
      const preset = category.presets.find(item => auditWaveForPreset(item) === wave);
      if (preset) return preset;
    }
    return null;
  }

  function renderAuditQuickActions() {
    const list = panel?.querySelector('#acb-audit-quick-list');
    if (!list) return;

    const specs = [
      { wave: 'core', index: '1', label: 'Audit Core', desc: 'Correctness + root causes' },
      { wave: 'second', index: '2', label: 'Second Wave', desc: 'Edges + lifecycle + recovery' },
      { wave: 'performance', index: '3', label: 'Performance', desc: 'Latency + stability + waste' }
    ];

    list.textContent = '';
    for (const spec of specs) {
      const preset = findAuditPreset(spec.wave);
      const row = document.createElement('div');
      row.className = 'acb-audit-quick-row';
      row.dataset.wave = spec.wave;
      setHTML(row, `
        <div class="acb-audit-wave-index">${spec.index}</div>
        <div class="acb-audit-wave-copy">
          <div class="acb-audit-wave-name">${escapeHTML(preset?.name || spec.label)}</div>
          <div class="acb-audit-wave-desc">${escapeHTML(preset?.desc || spec.desc)}</div>
        </div>
        <button type="button" data-quick-action="append" ${preset ? '' : 'disabled'} title="Prepare this wave in the ChatGPT composer without sending.">Prepare</button>
        <button type="button" data-quick-action="run" ${preset ? '' : 'disabled'} title="Prepare and send this wave now.">Run</button>
      `);
      list.appendChild(row);
    }
  }

  function renderCommands() {
    const list = panel?.querySelector('#acb-command-list');
    const filter = panel?.querySelector('#acb-filter');
    const section = panel?.querySelector('#acb-other-commands');
    const filterWrap = panel?.querySelector('#acb-filter-wrap');
    if (!list || !filter || !section || !filterWrap) return;

    renderAuditQuickActions();

    const category = activeCategory();
    const query = filter.value.trim().toLowerCase();
    const hasAnyCustom = state.categories.some(item =>
      item.presets.some(preset => !auditWaveForPreset(preset))
    );

    filterWrap.hidden = !hasAnyCustom;
    const presets = category
      ? category.presets.filter(preset =>
        !auditWaveForPreset(preset) &&
        (!query || `${preset.name}\n${preset.desc}\n${preset.text}`.toLowerCase().includes(query))
      )
      : [];

    section.hidden = !hasAnyCustom;
    list.textContent = '';

    if (!hasAnyCustom) return;

    if (!presets.length) {
      const empty = document.createElement('div');
      empty.className = 'acb-empty';
      empty.textContent = query
        ? 'No custom commands match this filter.'
        : 'No custom commands in this category.';
      list.appendChild(empty);
      return;
    }

    for (const preset of presets) {
      const row = document.createElement('div');
      row.className = 'acb-command-row';
      row.dataset.presetId = preset.id;
      setHTML(row, `
        <div class="acb-command-name" title="${escapeHTML(preset.desc || preset.text)}">${escapeHTML(preset.name)}</div>
        <button type="button" data-action="append" aria-label="Prepare ${escapeHTML(preset.name)} in composer">Prepare</button>
        <button type="button" data-action="run" aria-label="Prepare ${escapeHTML(preset.name)} and send">Run</button>
      `);
      list.appendChild(row);
    }
  }

  function renderManageCategory() {
    const select = panel?.querySelector('#acb-manage-category');
    const nameInput = panel?.querySelector('#acb-category-name');
    if (!select || !nameInput) return;
    select.textContent = '';
    for (const category of state.categories) {
      const option = document.createElement('option');
      option.value = category.id;
      option.textContent = category.name;
      option.selected = category.id === state.activeCategoryId;
      select.appendChild(option);
    }
    nameInput.value = activeCategory()?.name || '';
  }

  function renderManageList() {
    const list = panel?.querySelector('#acb-manage-list');
    if (!list) return;
    const category = activeCategory();
    list.textContent = '';
    if (!category || !category.presets.length) {
      const empty = document.createElement('div');
      empty.className = 'acb-empty';
      empty.textContent = 'No commands in this category.';
      list.appendChild(empty);
      return;
    }

    category.presets.forEach((preset, index) => {
      const row = document.createElement('div');
      row.className = 'acb-manage-row';
      row.dataset.presetId = preset.id;
      setHTML(row, `
        <div class="acb-manage-name" title="${escapeHTML(preset.desc || preset.text)}">${escapeHTML(preset.name)}</div>
        <div class="acb-manage-actions">
          <button type="button" data-manage="edit">Edit</button>
          <button type="button" data-manage="up" title="Move command up.">Move up</button>
          <button type="button" data-manage="down" title="Move command down.">Move down</button>
          <button type="button" data-manage="delete">Delete</button>
        </div>
      `);
      list.appendChild(row);
    });
  }

  function showEditor(presetId = null) {
    const editor = panel?.querySelector('#acb-editor');
    const title = panel?.querySelector('#acb-editor-title');
    const name = panel?.querySelector('#acb-edit-name');
    const desc = panel?.querySelector('#acb-edit-desc');
    const text = panel?.querySelector('#acb-edit-text');
    if (!editor || !title || !name || !desc || !text) return;

    const category = activeCategory();
    const preset = presetId && category ? category.presets.find(item => item.id === presetId) : null;
    editingPresetId = preset ? preset.id : null;
    title.textContent = preset ? `Edit command: ${preset.name}` : 'Add command';
    name.value = preset ? preset.name : '';
    desc.value = preset ? preset.desc : '';
    text.value = preset ? preset.text : '';
    name.classList.remove('acb-error');
    text.classList.remove('acb-error');
    editor.hidden = false;
    setStatus(preset ? `Editing command: ${preset.name}.` : 'New command editor opened.', 'info');
  }

  function hideEditor() {
    const editor = panel?.querySelector('#acb-editor');
    if (!editor) return;
    editor.hidden = true;
    editingPresetId = null;
  }

  function saveEditor() {
    const category = activeCategory();
    const nameInput = panel?.querySelector('#acb-edit-name');
    const descInput = panel?.querySelector('#acb-edit-desc');
    const textInput = panel?.querySelector('#acb-edit-text');
    if (!category || !nameInput || !descInput || !textInput) return;

    const name = nameInput.value.trim();
    const desc = descInput.value.trim();
    const text = textInput.value.trim();
    nameInput.classList.toggle('acb-error', !name);
    textInput.classList.toggle('acb-error', !text);

    if (!name || !text) {
      setStatus('Command was not saved: Name and Prompt are required. Fill both labeled fields, then press Save.', 'error');
      return;
    }

    const duplicateName = category.presets.some(item =>
      item.id !== editingPresetId && item.name.toLowerCase() === name.toLowerCase()
    );
    if (duplicateName) {
      nameInput.classList.add('acb-error');
      setStatus(`Command was not saved: "${name}" already exists in ${category.name}. Use a unique command name and retry.`, 'error');
      return;
    }

    clearPendingAction();

    if (editingPresetId) {
      const preset = category.presets.find(item => item.id === editingPresetId);
      if (!preset) {
        setStatus('Command was not saved: the edited command no longer exists. Reopen it from the Manage list.', 'error');
        return;
      }
      if (!commitStateMutation(() => {
        preset.name = name.slice(0, 40);
        preset.desc = desc.slice(0, 100);
        preset.text = text;
      }, 'Command edit could not be persisted; the previous command was restored.')) return;
      renderCommands();
      renderManageList();
      hideEditor();
      setStatus(`Saved command: ${preset.name}.`, 'success');
      return;
    }

    if (category.presets.length >= MAX_PRESETS) {
      setStatus(`Command was not added: ${category.name} already has the ${MAX_PRESETS}-command limit. Delete or move a command first.`, 'error');
      return;
    }

    const preset = {
      id: uid(),
      name: name.slice(0, 40),
      desc: desc.slice(0, 100),
      text
    };
    if (!commitStateMutation(
      () => { category.presets.push(preset); },
      'New command could not be persisted; it was not added.'
    )) return;
    renderCommands();
    renderManageList();
    hideEditor();
    setStatus(`Added command: ${preset.name}.`, 'success');
  }

  function sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
  }

  function chatGPTComposerReceiptState(receipt) {
    const wanted = String(receipt || '').trim();
    if (!wanted) return 'no-receipt';
    const input = rawChatGPTComposerInput();
    if (!input) return 'composer-unavailable';
    return composerPlainText(input).includes(`${AUTO_SEND_RECEIPT_PREFIX}: ${wanted}`)
      ? 'present-with-receipt'
      : 'present-without-receipt';
  }

  function chatGPTComposerStillContainsReceipt(receipt) {
    return chatGPTComposerReceiptState(receipt) === 'present-with-receipt';
  }

  async function chatGPTSendAccepted(receipt = '', beforeText = '', timeoutMs = 900) {
    const wanted = String(receipt || '').trim();
    const baseline = cleanTurnText(beforeText || '');
    const deadline = Date.now() + Math.max(120, Number(timeoutMs) || 900);
    let observedPreparedComposer = false;

    while (Date.now() < deadline) {
      if (wanted) {
        if (exactReceiptUserTurn(wanted, getChatGPTTurns())) return true;
        const state = chatGPTComposerReceiptState(wanted);
        if (state === 'present-with-receipt') observedPreparedComposer = true;
        if (state === 'present-without-receipt' && observedPreparedComposer) return true;
        // composer-unavailable is deliberately UNKNOWN. React can replace the
        // composer during hydration without having submitted the authored turn.
      } else {
        const live = rawChatGPTComposerInput();
        if (live) {
          const current = cleanTurnText(composerPlainText(live));
          if (baseline && current !== baseline) return true;
        }
      }
      if (chatGPTIsGenerating()) return true;
      await sleep(60);
    }
    return false;
  }

  function dispatchElementClick(element) {
    if (!element) return;
    try {
      if (typeof PointerEvent === 'function') {
        element.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, cancelable: true, view: window }));
      }
      if (typeof MouseEvent === 'function') {
        element.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true, view: window }));
      }
      if (typeof PointerEvent === 'function') {
        element.dispatchEvent(new PointerEvent('pointerup', { bubbles: true, cancelable: true, view: window }));
      }
      if (typeof MouseEvent === 'function') {
        element.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true, view: window }));
      }
      element.click();
    } catch (_) {
      try { element.click(); } catch (_) { }
    }
  }

  async function clickChatGPTSendVerified(button, input, options = {}) {
    if (!button || !button.isConnected || button.disabled || button.getAttribute('aria-disabled') === 'true') {
      return false;
    }

    const receipt = String(options.receipt || '');
    const beforeText = composerPlainText(input || rawChatGPTComposerInput());
    const ownership = options.autoOwnership || null;
    const fence = typeof options.fence === 'function' ? options.fence : null;
    if (ownership && !(await ownership.verify())) return false;
    if (fence && !(await fence())) return false;

    try { button.focus({ preventScroll: true }); } catch (_) { }
    if (ownership && !(await ownership.verify())) return false;
    if (fence && !(await fence())) return false;
    dispatchElementClick(button);

    const innerTarget = button.querySelector('svg, path, span') || button;
    if (innerTarget !== button) {
      try { dispatchElementClick(innerTarget); } catch (_) { }
    }

    if (await chatGPTSendAccepted(receipt, beforeText, 2500)) return true;

    const liveInput = rawChatGPTComposerInput();
    const receiptState = receipt ? chatGPTComposerReceiptState(receipt) : '';
    const stillPrepared = receipt
      ? receiptState === 'present-with-receipt'
      : Boolean(liveInput && cleanTurnText(composerPlainText(liveInput)) === cleanTurnText(beforeText));

    // A temporarily missing composer is not send acceptance and also is not a
    // safe moment to requestSubmit an old/stale form reference.
    if (receipt && receiptState === 'composer-unavailable') return false;
    if (!receipt && !liveInput) return false;
    if (!stillPrepared) return true;
    if (ownership && !(await ownership.verify())) return false;
    if (fence && !(await fence())) return false;

    const form = button.closest('form') || liveInput?.closest('form');
    if (form && typeof form.requestSubmit === 'function') {
      try {
        if (button.closest('form') === form && button.getAttribute('type') === 'submit') {
          form.requestSubmit(button);
        } else {
          form.requestSubmit();
        }
      } catch (_) { }
      if (await chatGPTSendAccepted(receipt, beforeText, 2500)) return true;
    }

    if (liveInput) {
      try {
        liveInput.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true, cancelable: true }));
        liveInput.dispatchEvent(new KeyboardEvent('keyup', { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true, cancelable: true }));
      } catch (_) { }
      if (await chatGPTSendAccepted(receipt, beforeText, 2000)) return true;
    }

    return false;
  }

  async function triggerSend(site, input, options = {}) {
    const waitForReadyMs = Math.max(Number(options.waitForReadyMs) || 0, site?.key === 'chatgpt' ? 12000 : 0);
    const fence = typeof options.fence === 'function' ? options.fence : null;
    const beforeClick = typeof options.beforeClick === 'function'
      ? options.beforeClick
      : null;

    const fenceCheck = async () => {
      if (!fence) return true;
      try {
        return Boolean(await fence());
      } catch (_) {
        return false;
      }
    };

    const beforeClickCheck = async () => {
      if (!beforeClick) return true;
      try {
        return Boolean(await beforeClick());
      } catch (_) {
        return false;
      }
    };

    if (site?.key === 'chatgpt' && waitForReadyMs > 0) {
      const ready = await waitForChatGPTSendReady(waitForReadyMs);
      if (ready) {
        if (!(await fenceCheck())) return { ok: false, mode: 'ownership-lost' };
        if (!(await beforeClickCheck())) return { ok: false, mode: 'pre-click-checkpoint-failed' };
        const accepted = site?.key === 'chatgpt'
          ? await clickChatGPTSendVerified(ready, input, { receipt: options.receipt, fence: fenceCheck })
          : (ready.click(), true);
        return accepted
          ? { ok: true, mode: 'button' }
          : { ok: false, mode: 'click-unverified' };
      }
    } else {
      const retryDelays = [0, 35, 70, 120, 180, 260];
      for (const delay of retryDelays) {
        if (delay) await sleep(delay);
        const button = cachedSiteElement(site, 'send');
        if (button && !button.disabled && button.getAttribute('aria-disabled') !== 'true') {
          if (!(await fenceCheck())) return { ok: false, mode: 'ownership-lost' };
          if (!(await beforeClickCheck())) return { ok: false, mode: 'pre-click-checkpoint-failed' };
          const accepted = site?.key === 'chatgpt'
            ? await clickChatGPTSendVerified(button, input, { receipt: options.receipt, fence: fenceCheck })
            : (button.click(), true);
          return accepted
            ? { ok: true, mode: 'button' }
            : { ok: false, mode: 'click-unverified' };
        }
      }
    }

    if (site.allowEnterFallback === false) {
      return { ok: false, mode: 'manual-only' };
    }

    if (!(await fenceCheck())) return { ok: false, mode: 'ownership-lost' };
    if (!(await beforeClickCheck())) return { ok: false, mode: 'pre-click-checkpoint-failed' };

    try {
      input.dispatchEvent(new KeyboardEvent('keydown', {
        key: 'Enter',
        code: 'Enter',
        keyCode: 13,
        which: 13,
        bubbles: true,
        cancelable: true,
        shiftKey: false
      }));
      input.dispatchEvent(new KeyboardEvent('keyup', {
        key: 'Enter',
        code: 'Enter',
        keyCode: 13,
        which: 13,
        bubbles: true,
        cancelable: true,
        shiftKey: false
      }));
      return { ok: false, mode: 'enter-fallback' };
    } catch (_) {
      return { ok: false, mode: 'failed' };
    }
  }

  async function executePreset(preset, mode, options = {}) {
    if (actionInFlight) {
      if (!options.quietBusy) {
        setStatus('A command action is already running. Wait for that action to finish before triggering another one.', 'warning');
      }
      return { ok: false, sent: false, reason: 'busy' };
    }

    actionInFlight = true;
    try {
      const site = detectSite();
      const siteLabel = site.label;
      const input = cachedSiteElement(site, 'input');
      if (!input) {
        setStatus(`Composer not found on ${siteLabel}. Open a chat composer on this page, then press ${mode === 'run' ? 'Run' : 'Append'} again.`, 'error');
        return { ok: false, sent: false, reason: 'composer-not-found' };
      }

      setStatus(`${mode === 'run' ? 'Running' : 'Appending'} ${preset.name}...`, 'info');
      await yieldToBrowser();

      const inputValidator = site.validateInput;
      if (!input.isConnected || !isVisible(input) || (inputValidator && !inputValidator(input))) {
        elementCache.input = null;
        elementCache.send = null;
        setStatus(`Composer changed before insertion on ${siteLabel}. No text was written. Close message editing or restore the main composer, then retry.`, 'error');
        return { ok: false, sent: false, reason: 'composer-changed' };
      }

      if (site?.key === 'chatgpt' && auditKindFromPreset(preset) === 'core') {
        reconcileProjectIdentityFromComposer({ rename: true });
      }

      const attachmentDelivery = shouldUseChatGPTPromptAttachment(site, preset);
      let attachment = null;
      let deliveryText = '';
      let canonicalExpectedText = '';

      const canonicalComposerMatches = () => {
        if (!options.canonicalComposerOnly) return true;
        return cleanTurnText(composerPlainText(input)) === cleanTurnText(canonicalExpectedText);
      };

      if (options.autoOwnership && !(await options.autoOwnership.verify())) {
        setStatus(`Automatic send for ${preset.name} was canceled because this tab lost Auto3 ownership or the composer was changed before insertion. Nothing was written or sent.`, 'warning');
        return { ok: false, sent: false, reason: 'ownership-lost' };
      }

      if (attachmentDelivery) {
        setStatus(`Attaching ${preset.name} as a prompt file so ChatGPT does not parse the full text in ProseMirror...`, 'info');
        attachment = await attachChatGPTPromptFile(preset, input, {
          canonicalComposerOnly: Boolean(options.canonicalComposerOnly)
        });
        if (!attachment.ok) {
          setStatus(`Could not attach ${preset.name} as a file (${attachment.reason}). The large prompt was NOT pasted into the editor, preventing a UI freeze. Change Delivery to Text only if you explicitly want raw text.`, 'error');
          return { ok: false, sent: false, reason: attachment.reason, delivery: 'attachment' };
        }
        canonicalExpectedText = attachment.marker || '';
      } else {
        if (options.canonicalComposerOnly && cleanTurnText(composerPlainText(input))) {
          setStatus(`Automatic ${preset.name} was canceled because the composer changed before canonical insertion. Existing text was not merged into the audit command.`, 'warning');
          return { ok: false, sent: false, reason: 'canonical-composer-changed' };
        }
        deliveryText = preset?.machineReceipt
          ? `${preset.text}\n\n${AUTO_SEND_RECEIPT_PREFIX}: ${preset.machineReceipt}`
          : preset.text;
        const appended = smartAppend(input, deliveryText);
        if (!appended) {
          elementCache.input = null;
          setStatus(`Could not write to the ${siteLabel} composer. The page editor rejected scripted input; click the composer once and retry.`, 'error');
          return { ok: false, sent: false, reason: 'write-rejected' };
        }
        canonicalExpectedText = deliveryText;
      }

      if (options.canonicalComposerOnly && !canonicalComposerMatches()) {
        setStatus(`Automatic ${preset.name} was canceled because unexpected composer text appeared during preparation. Nothing was sent.`, 'warning');
        return { ok: false, sent: false, reason: 'canonical-composer-changed' };
      }

      if (mode === 'append') {
        if (attachmentDelivery) {
          setStatus(`Attached ${preset.name} as ${attachment.filename} and added only a short instruction marker. Existing composer text/files were preserved.`, 'success');
          return { ok: true, sent: false, mode: 'append-attachment', delivery: 'attachment', filename: attachment.filename };
        }
        setStatus(`Appended ${preset.name} to the ${siteLabel} composer. Existing composer text was preserved.`, 'success');
        return { ok: true, sent: false, mode: 'append', delivery: 'text' };
      }

      if (options.autoOwnership) options.autoOwnership.captureWrite();

      setStatus(attachmentDelivery
        ? `Attached ${preset.name} as ${attachment.filename}. Waiting for ChatGPT to finish registering the attachment and enable Send...`
        : `Inserted ${preset.name}. Waiting briefly for the ${siteLabel} Send control...`, 'info');
      await yieldToBrowser();

      if (options.canonicalComposerOnly && !canonicalComposerMatches()) {
        setStatus(`Automatic ${preset.name} was canceled because the composer changed after preparation. Nothing was sent.`, 'warning');
        return { ok: false, sent: false, reason: 'canonical-composer-changed' };
      }

      if (typeof options.beforeSend === 'function') {
        let permitted = false;
        try {
          if (options.autoOwnership && !(await options.autoOwnership.verify())) {
            setStatus(`Automatic Send for ${preset.name} was canceled because this tab lost Auto3 ownership while the composer was being prepared. Nothing was sent.`, 'warning');
            return { ok: false, sent: false, reason: 'ownership-lost' };
          }
          permitted = await options.beforeSend({ site, input, preset, attachmentDelivery, attachment });
        } catch (_) {
          permitted = false;
        }
        if (!permitted) {
          setStatus(`Automatic Send for ${preset.name} was canceled because its durable pre-send checkpoint could not be saved. The prepared composer content was left untouched.`, 'error');
          return { ok: false, sent: false, reason: 'pre-send-checkpoint-failed' };
        }
      }

      const result = await triggerSend(site, input, {
        waitForReadyMs: attachmentDelivery ? CHATGPT_ATTACHMENT_TIMEOUT_MS : 12000,
        fence: (options.autoOwnership || options.canonicalComposerOnly)
          ? async () => {
            if (options.canonicalComposerOnly && !canonicalComposerMatches()) return false;
            if (options.autoOwnership && !(await options.autoOwnership.verify())) return false;
            return true;
          }
          : undefined,
        beforeClick: options.beforeClick,
        receipt: String(preset?.machineReceipt || '')
      });
      if (result.mode === 'ownership-lost') {
        setStatus(`Automatic Send for ${preset.name} was canceled immediately before the click because this tab no longer owns Auto3 or the composer content changed. Nothing was sent.`, 'warning');
        return { ok: false, sent: false, reason: 'ownership-lost' };
      }
      if (result.mode === 'pre-click-checkpoint-failed') {
        setStatus(`Send for ${preset.name} was canceled before the click because its final durable START checkpoint could not be saved. Nothing was sent.`, 'error');
        return { ok: false, sent: false, reason: 'pre-click-checkpoint-failed' };
      }
      if (result.ok) {
        setStatus(`Run triggered: ${preset.name}. The ${siteLabel} Send control was clicked.`, 'success');
        if (site?.key === 'chatgpt' && auditKindFromPreset(preset) === 'core') {
          const sentReceipt = String(preset?.machineReceipt || '');
          setTimeout(() => {
            try {
    bindAutoRuntimeToCurrentConversation({ claim: false });
    try { console.log('[ACB-DEBUG] after bind', { enabled: autoRuntime?.enabled, stage: autoRuntime?.stage, key: autoBoundConversationKey, attachmentReady: chatGPTReadyAttachmentSummary() }); } catch (_) {}

              const turns = getChatGPTTurns();
              const latestUser = latestChatGPTUserTurn(turns);
              if (!latestUser || classifyAuditTurn(latestUser) !== 'core') return;
              if (sentReceipt && !userTurnContainsReceipt(latestUser, sentReceipt)) return;
              const projectName = projectNameFromCoreTurn(latestUser);
              if (!projectName) return;
              updateRuntimeProjectName(projectName, 'artifact');
              maybeRenameConversation(projectName, {
                source: 'artifact',
                conversationKey: currentConversationKey(),
                runStartedAt: autoRuntime?.startedAt || 0
              }).catch(() => { });
            } catch (_) { }
          }, 700);
        }
        return { ok: true, sent: true, mode: result.mode };
      }

      if (result.mode === 'click-unverified') {
        setStatus(`The live ${siteLabel} Send control was found and invoked, but the prepared prompt did not leave the composer. START keeps the exact receipt armed and will retry through the alternate submit path instead of pretending Send succeeded.`, 'warning');
        return { ok: false, sent: false, mode: result.mode, reason: 'send-click-unverified' };
      }

      if (result.mode === 'enter-fallback') {
        setStatus(`Prompt was inserted, but the ${siteLabel} Send control did not become ready. Enter fallback was triggered; verify the site accepted it, otherwise press Send manually.`, 'warning');
        return { ok: false, sent: false, mode: result.mode, reason: 'unverified-enter-fallback' };
      }

      if (result.mode === 'manual-only') {
        setStatus(`Prompt was inserted into the verified ${siteLabel} composer, but its Send control did not become ready. Automatic Enter fallback is disabled on this site to prevent sending or editing the wrong field. Press Send manually.`, 'warning');
        return { ok: false, sent: false, mode: result.mode, reason: 'manual-send-required' };
      }

      setStatus(`Prompt was inserted, but ${siteLabel} could not be sent automatically. Press the site's Send control manually.`, 'warning');
      return { ok: false, sent: false, mode: result.mode, reason: 'send-failed' };
    } catch (error) {
      setStatus(`Command action failed: ${error?.message || 'unexpected runtime error'}. Retry once; if it repeats, use Append and send manually.`, 'error');
      return { ok: false, sent: false, reason: 'exception', error };
    } finally {
      actionInFlight = false;
    }
  }


  function auditKindFromStep(step) {
    const number = Number(step);
    if (number === 1) return 'core';
    if (number === 2) return 'second';
    if (number === 3) return 'performance';
    return '';
  }

  function auditWaveTitle(kind) {
    if (kind === 'core') return 'AUDIT CORE';
    if (kind === 'second') return 'AUDIT SECOND WAVE';
    if (kind === 'performance') return 'AUDIT PERFORMANCE / STABILITY / EFFECTIVENESS';
    return 'AUDIT';
  }

  function auditResultStorageKey(conversationKey, kind) {
    return `${AUTO_AUDIT_RESULT_PREFIX}${String(conversationKey || 'unknown')}:${String(kind || 'unknown')}`;
  }

  function readAuditResultFresh(kind, conversationKey = autoBoundConversationKey || currentConversationKey()) {
    const wantedKind = String(kind || '');
    const wantedConversationKey = String(conversationKey || '');
    if (!['core', 'second', 'performance'].includes(wantedKind) || !wantedConversationKey) return null;
    const key = auditResultStorageKey(wantedConversationKey, wantedKind);
    try {
      const raw = GM_getValue(key, null);
      const parsed = raw ? JSON.parse(raw) : null;
      if (
        !parsed ||
        parsed.version !== 1 ||
        String(parsed.kind || '') !== wantedKind ||
        String(parsed.conversationKey || '') !== wantedConversationKey ||
        typeof parsed.text !== 'string' ||
        !parsed.text.trim()
      ) return null;
      return parsed;
    } catch (_) {
      return null;
    }
  }

  function readAuditResult(kind, conversationKey = autoBoundConversationKey || currentConversationKey()) {
    if (!kind || !conversationKey) return null;
    const key = auditResultStorageKey(conversationKey, kind);
    if (auditResultCache.has(key)) return auditResultCache.get(key);
    const record = readAuditResultFresh(kind, conversationKey);
    auditResultCache.set(key, record);
    return record;
  }

  function signalAuditResultChange(conversationKey = '', kind = '') {
    if (typeof GM_setValue !== 'function') return false;
    try {
      GM_setValue(AUTO_AUDIT_RESULT_SIGNAL_KEY, JSON.stringify({
        conversationKey: String(conversationKey || ''),
        kind: String(kind || ''),
        source: autoInstanceId,
        at: Date.now(),
        nonce: Math.random().toString(36).slice(2, 10)
      }));
      return true;
    } catch (_) {
      return false;
    }
  }

  function invalidateAuditResultCache(conversationKey = '') {
    const wanted = String(conversationKey || '');
    if (!wanted) {
      auditResultCache.clear();
      return;
    }
    for (const kind of ['core', 'second', 'performance']) {
      auditResultCache.delete(auditResultStorageKey(wanted, kind));
    }
  }

  function installAuditResultListener() {
    if (auditResultListenerId !== null || typeof GM_addValueChangeListener !== 'function') return;
    try {
      auditResultListenerId = GM_addValueChangeListener(
        AUTO_AUDIT_RESULT_SIGNAL_KEY,
        (_name, _oldValue, newValue, remote) => {
          // Tampermonkey reports remote=false for this same userscript instance.
          // Ignore that echo: writeAuditResult() already updated its local cache.
          if (remote === false) return;

          let conversationKey = '';
          try {
            const payload = newValue ? JSON.parse(String(newValue)) : null;
            conversationKey = String(payload?.conversationKey || '');
          } catch (_) { }

          invalidateAuditResultCache(conversationKey);
          renderAutoAuditState();

          const currentKey = autoBoundConversationKey || currentConversationKey();
          if (
            autoRuntime?.enabled &&
            autoRuntime.stage === 'idle' &&
            (!conversationKey || conversationKey === currentKey)
          ) {
            scheduleAutoAuditCheck(120);
          }
        }
      );
    } catch (_) {
      auditResultListenerId = null;
    }
  }

  function auditResultWriteMatchesCurrentRuntime(record) {
    if (!record?.conversationKey) return false;

    // Cross-tab callbacks can outlive the run that created them. The durable
    // per-conversation runtime is the authority for whether a late result is
    // still allowed to occupy the one-record-per-wave cache key.
    const stored = readStoredRuntime(record.conversationKey);
    const runtime = stored.runtime;
    if (stored.corrupt) return false;
    if (!runtime) return true;

    // Explicit Reset is a durable lineage barrier. Until a fresh Core arms a
    // new run, no historical tab/callback may resurrect cached audit evidence.
    if (runtime.resetBarrierActive) return false;

    const runtimeRunId = String(runtime.runId || '');
    const recordRunId = String(record.runId || '');
    if (runtimeRunId && runtimeRunId !== recordRunId) return false;
    return true;
  }

  function writeAuditResult(record) {
    if (
      !record?.conversationKey ||
      !['core', 'second', 'performance'].includes(String(record?.kind || '')) ||
      typeof record?.text !== 'string' ||
      !record.text.trim()
    ) return false;
    if (!auditResultWriteMatchesCurrentRuntime(record)) return false;

    const key = auditResultStorageKey(record.conversationKey, record.kind);
    try {
      const payload = JSON.stringify(record);
      GM_setValue(key, payload);
      const verified = GM_getValue(key, null);
      if (verified !== payload) throw new Error('audit-result read-back mismatch');
      auditResultCache.set(key, record);
      signalAuditResultChange(record.conversationKey, record.kind);
      return true;
    } catch (error) {
      setStatus(`Completed ${auditWaveTitle(record.kind)} could not be cached for one-click copy: ${error?.message || 'userscript storage failure'}. Auto3 itself will continue.`, 'warning');
      return false;
    }
  }

  function patchAuditResult(
    kind,
    mutator,
    conversationKey = autoBoundConversationKey || currentConversationKey(),
    options = {}
  ) {
    // Read fresh from GM storage for every read-modify-write. A standby tab can
    // otherwise overwrite bridge/save fields written by the active tab from a
    // stale in-memory cache after lease ownership changes.
    const current = readAuditResultFresh(kind, conversationKey);
    if (!current) return false;

    // Async bridge/folder callbacks carry the run that initiated the I/O. Never
    // let an old Core save/retry response mutate a newer Core record that reused
    // the same conversation+wave storage key.
    const expectedRunId = String(options.expectedRunId || '');
    if (expectedRunId && String(current.runId || '') !== expectedRunId) return false;

    const next = { ...current };
    mutator(next);
    return writeAuditResult(next);
  }

  function clearAuditResultsForConversation(conversationKey = autoBoundConversationKey || currentConversationKey()) {
    if (!conversationKey) return false;
    let cleared = true;
    for (const kind of ['core', 'second', 'performance']) {
      const key = auditResultStorageKey(conversationKey, kind);
      auditResultCache.delete(key);
      try { GM_deleteValue(key); } catch (_) { cleared = false; }
      try {
        const leftover = GM_getValue(key, null);
        if (leftover !== null && leftover !== undefined && leftover !== '') {
          try { GM_setValue(key, ''); } catch (_) { }
          const verified = GM_getValue(key, null);
          if (verified !== null && verified !== undefined && verified !== '') cleared = false;
        }
      } catch (_) { cleared = false; }
    }
    signalAuditResultChange(conversationKey, '');
    return cleared;
  }

  function cleanProjectName(value) {
    return String(value || '')
      .replace(/[`<>"|?*]/g, '')
      .replace(/[\\/:]+/g, '-')
      .replace(/\s+/g, ' ')
      .trim()
      .slice(0, 80);
  }



  function looksOpaqueIdentity(value) {
    const cleaned = cleanProjectName(value);
    if (!cleaned) return false;

    const compact = cleaned.replace(/\s+/g, '');
    if (/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(compact)) return true;
    if (/^(?:file|asset|upload|attachment)[_-]?[0-9a-z_-]{12,}$/i.test(compact)) return true;
    if (/^[0-9a-f]{12,64}$/i.test(compact)) return true;
    if (/^[0-9a-z]{8,}(?:-[0-9a-z]{4,}){2,}$/i.test(compact) && /\d/.test(compact)) return true;
    if (/^\d{6,}$/.test(compact)) return true;
    return false;
  }

  function sanitizeProjectIdentity(value) {
    const cleaned = cleanProjectName(value);
    if (!cleaned) return '';

    if (/^(?:PROJECT|CHAT|UNTITLED|UNKNOWN|N\/?A|NONE|NULL|NEW CHAT|CHATGPT)$/i.test(cleaned)) return '';
    if (/^(?:SKIP TO (?:CONTENT|MAIN CONTENT)|VIEW CHAT|IMAGES|PLUGINS|DEEP RESEARCH|SETTINGS|HELP|LOG IN|SIGN UP|SEE PLANS AND PRICING)$/i.test(cleaned)) return '';
    if (looksOpaqueIdentity(cleaned)) return '';
    if (/^(?:https?:\/\/|blob:|data:)/i.test(cleaned)) return '';
    return cleaned;
  }


  function supportedProjectAttachmentFilename(filename) {
    const raw = String(filename || '').trim();
    if (!raw) return false;
    if (/\.(?:tar\.gz|zip|7z|rar|tgz|tar)$/i.test(raw)) return true;
    return /\.(?:js|mjs|cjs|ts|tsx|jsx|py|rs|go|java|kt|kts|cs|cpp|cc|c|h|hpp|toml|yaml|yml|json|jsonc|ini|cfg|conf|md|txt|ps1|sh|bat|cmd|html|css|scss|vue|svelte)$/i.test(raw);
  }

  function projectNameFromAuditText(text) {
    const match = String(text || '').match(/^\s*PROJECT_NAME\s*:\s*(.+?)\s*$/im);
    return sanitizeProjectIdentity(match?.[1] || '');
  }

  function projectNameFromArtifactFilename(filename) {
    let base = String(filename || '').trim().replace(/^.*[\\/]/, '');
    base = base.replace(/\.(?:tar\.gz|zip|7z|rar|tgz|tar)$/i, '');
    base = base.replace(/\s*\(\d+\)\s*$/, '');
    base = base.replace(/(?:[_\s-]+)(?:\d{2}-\d{2}-\d{4}|\d{4}-\d{2}-\d{2})(?:[-_T].*)?$/i, '');
    base = base.replace(/[_\s-]+T?\d{1,2}[-_:]\d{2}(?:[-_:]\d{2})?.*$/i, '');
    return sanitizeProjectIdentity(base);
  }

  function projectNameFromAttachmentFilename(filename) {
    const raw = String(filename || '').trim().replace(/^.*[\\/]/, '');
    if (!raw || /^AUDIT_(?:CORE|SECOND_WAVE|PERFORMANCE)_/i.test(raw)) return '';
    if (!supportedProjectAttachmentFilename(raw)) return '';

    if (/\.(?:tar\.gz|zip|7z|rar|tgz|tar)$/i.test(raw)) {
      return projectNameFromArtifactFilename(raw);
    }

    let base = raw.replace(/\.[A-Za-z0-9]{1,12}$/i, '');
    base = base.replace(/\s*\(\d+\)\s*$/, '');
    base = base.replace(/(?:[_\s-]+)(?:\d{2}-\d{2}-\d{4}|\d{4}-\d{2}-\d{2})(?:[-_T].*)?$/i, '');
    base = base.replace(/[_\s-]+T?\d{1,2}[-_:]\d{2}(?:[-_:]\d{2})?.*$/i, '');
    if (/^(README|LICENSE|UI|CORE|STATE|BOARD|LOG)$/i.test(base)) return '';
    return sanitizeProjectIdentity(base);
  }

  function projectNameFromComposerAttachments() {
    if (detectSite().key !== 'chatgpt') return '';

    const summary = chatGPTReadyAttachmentSummary();
    const names = Array.isArray(summary?.names) ? summary.names.filter(Boolean) : [];
    if (!names.length) return '';

    const archives = names.filter(name => /\.(?:tar\.gz|zip|7z|rar|tgz|tar)$/i.test(name));
    const others = names.filter(name => !/\.(?:tar\.gz|zip|7z|rar|tgz|tar)$/i.test(name));

    for (const name of [...archives, ...others]) {
      const project = projectNameFromAttachmentFilename(name);
      if (project) return project;
    }
    return '';
  }

  function reconcileProjectIdentityFromComposer(options = {}) {
    if (detectSite().key !== 'chatgpt' || !autoRuntime) return '';

    const detected = projectNameFromComposerAttachments();
    if (!detected) return '';

    const project = updateRuntimeProjectName(detected, 'artifact');
    if (!project) return '';

    renderAutoAuditState();

    if (state?.autoRenameChat && options.rename !== false) {
      const renameContext = {
        source: 'artifact',
        conversationKey: autoBoundConversationKey || currentConversationKey(),
        runStartedAt: autoRuntime.startedAt || 0
      };
      maybeRenameConversation(project, renameContext).catch(() => { });
      scheduleConversationTitleGuard(project, renameContext);
    }

    return project;
  }

  function projectNameFromCoreTurn(turn) {
    if (!turn) return '';
    const haystacks = [readableNodeText(turn), String(turn.textContent || '')];
    for (const node of turn.querySelectorAll?.('[aria-label], [title]') || []) {
      haystacks.push(String(node.getAttribute('aria-label') || ''));
      haystacks.push(String(node.getAttribute('title') || ''));
    }

    const filenames = [];
    const seen = new Set();
    const archivePattern = /([^\n"'<>]{1,180}?\.(?:tar\.gz|zip|7z|rar|tgz|tar))\b/gi;
    for (const haystack of haystacks) {
      let match;
      while ((match = archivePattern.exec(haystack))) {
        const name = match[1].trim().replace(/^.*[\\/]/, '');
        if (!name || seen.has(name.toLowerCase())) continue;
        seen.add(name.toLowerCase());
        filenames.push(name);
      }
    }

    // Prefer the real project archive over generated audit-command attachments.
    const target = filenames.find(name => !/^AUDIT[_\s-]/i.test(name)) || filenames[0] || '';
    if (target) return projectNameFromArtifactFilename(target);

    // Single-file audits have no archive. Use a visible non-command implementation
    // filename only as an early guess; PROJECT_NAME from the Core handoff will
    // replace it later with authoritative identity.
    const genericPattern = /([^\n"'<>]{1,160}?\.(?:js|mjs|cjs|ts|tsx|jsx|py|rs|go|java|cs|cpp|c|h|hpp|json|toml|yaml|yml|md))\b/gi;
    for (const haystack of haystacks) {
      let match;
      while ((match = genericPattern.exec(haystack))) {
        const name = match[1].trim().replace(/^.*[\/]/, '');
        if (/^AUDIT[_\s-]/i.test(name)) continue;
        const base = name.replace(/\.[^.]+$/, '');
        if (/^(README|LICENSE|UI|CORE|STATE|BOARD|LOG)$/i.test(base)) continue;
        return sanitizeProjectIdentity(base);
      }
    }
    return '';
  }

  function updateRuntimeProjectName(name, source = '') {
    const cleaned = sanitizeProjectIdentity(name);
    if (!cleaned || !autoRuntime) return '';
    const sourceRank = { '': 0, artifact: 1, handoff: 2 };
    const currentRank = sourceRank[autoRuntime.projectNameSource] || 0;
    const nextRank = sourceRank[source] || 0;
    if (!autoRuntime.projectName || nextRank >= currentRank) {
      autoRuntime.projectName = cleaned;
      autoRuntime.projectNameSource = source;
      saveAutoRuntime({ pauseOnFailure: false });
    }
    return autoRuntime.projectName;
  }

  function safeFilePart(value, fallback = 'PROJECT') {
    const cleaned = cleanProjectName(value)
      .replace(/[^\p{L}\p{N}._ -]+/gu, '_')
      .replace(/\s+/g, '_')
      .replace(/_+/g, '_')
      .replace(/^[_ .-]+|[_ .-]+$/g, '');
    return (cleaned || fallback).slice(0, 80);
  }

  function auditRunStamp(timestamp = autoRuntime?.startedAt || Date.now()) {
    const date = new Date(Number(timestamp) || Date.now());
    const pad = value => String(value).padStart(2, '0');
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}-${pad(date.getMinutes())}-${pad(date.getSeconds())}`;
  }

  function auditResultFilename(record) {
    const prefix = record.kind === 'core' ? '01_AUDIT_CORE'
      : record.kind === 'second' ? '02_AUDIT_SECOND_WAVE'
        : '03_AUDIT_PERFORMANCE';
    return `${safeFilePart(record.projectName)}__${prefix}__${auditRunStamp(record.runStartedAt)}.md`;
  }

  function combinedAuditFilename(projectName, runStartedAt) {
    return `${safeFilePart(projectName)}__00_AUDIT_ALL_3__${auditRunStamp(runStartedAt)}.md`;
  }

  async function copyAuditText(text) {
    const value = String(text || '');
    if (!value) return false;
    try {
      if (typeof GM_setClipboard === 'function') {
        GM_setClipboard(value, 'text');
        return true;
      }
    } catch (_) { }
    try {
      await navigator.clipboard.writeText(value);
      return true;
    } catch (_) {
      return false;
    }
  }

  async function copyCompletedAudit(kind) {
    const record = currentChatAuditRecords().find(item => item.kind === kind) || null;
    if (!record?.text) {
      setStatus(`${auditWaveTitle(kind)} is not cached as a completed audit yet. Let that wave reach COMPLETE first.`, 'warning');
      return false;
    }
    const copied = await copyAuditText(record.text);
    if (!copied) {
      setStatus(`${auditWaveTitle(kind)} is ready, but clipboard access was rejected by the browser.`, 'error');
      return false;
    }
    copiedAuditKind = kind;
    copiedAuditUntil = Date.now() + 1400;
    if (copiedAuditTimer) clearTimeout(copiedAuditTimer);
    copiedAuditTimer = setTimeout(() => {
      copiedAuditKind = '';
      copiedAuditUntil = 0;
      copiedAuditTimer = 0;
      renderAutoAuditState();
    }, 1450);

    renderAutoAuditState();
    setStatus(`${auditWaveTitle(kind)} COPIED (${record.text.length.toLocaleString()} characters).`, 'success');
    return true;
  }


  function createAuditRunId() {
    const random = globalThis.crypto && typeof globalThis.crypto.randomUUID === 'function'
      ? globalThis.crypto.randomUUID().replace(/-/g, '')
      : `${Math.random().toString(36).slice(2)}${Math.random().toString(36).slice(2)}`;
    return `acb-${Date.now().toString(36)}-${random.slice(0, 20)}`;
  }

  // SECURITY (CORE-001): never embed a live Bridge credential in the distributable
  // widget. Token provisioning is user-mediated via Tampermonkey/private storage
  // (GM_getValue/GM_setValue). An empty placeholder means "no token configured";
  // the user must paste the token once, and /widget.user.js is served token-free.
  const INJECTED_BRIDGE_TOKEN = '';

  function bridgeToken() {
    try {
      const stored = String(GM_getValue(BRIDGE_TOKEN_KEY, '') || '').trim();
      if (stored) return stored;
    } catch (_) { }
    return '';
  }

  function saveBridgeToken(value) {
    const token = String(value || '').trim();
    try {
      if (token) GM_setValue(BRIDGE_TOKEN_KEY, token);
      else GM_deleteValue(BRIDGE_TOKEN_KEY);

      const verified = String(GM_getValue(BRIDGE_TOKEN_KEY, '') || '').trim();
      if (verified !== token) throw new Error('token storage read-back mismatch');
      return true;
    } catch (error) {
      setStatus(`Bridge token could not be saved: ${error?.message || 'userscript storage failure'}.`, 'error');
      return false;
    }
  }

  function normalizedBridgeUrl(value = state?.bridgeUrl || BRIDGE_DEFAULT_URL) {
    try {
      const url = new URL(String(value || BRIDGE_DEFAULT_URL).trim());
      if (url.protocol !== 'http:') return '';
      const host = String(url.hostname || '').toLowerCase();
      if (host !== '127.0.0.1' && host !== 'localhost') return '';
      if (url.username || url.password || url.search || url.hash) return '';
      const basePath = url.pathname.replace(/\/+$/, '');
      if (basePath && basePath !== '/') return '';
      return `${url.protocol}//${url.host}`;
    } catch (_) {
      return '';
    }
  }

  function bridgeRequest(method, path, body = null, options = {}) {
    return new Promise(resolve => {
      const base = normalizedBridgeUrl();
      if (!base) {
        resolve({
          ok: false,
          status: 0,
          data: null,
          errorCode: 'invalid_bridge_url',
          retriable: false,
          message: 'Bridge URL must be loopback HTTP (127.0.0.1 or localhost).'
        });
        return;
      }

      const token = options.auth === false ? '' : bridgeToken();
      if (options.auth !== false && !token) {
        resolve({
          ok: false,
          status: 0,
          data: null,
          errorCode: 'invalid_auth',
          retriable: false,
          message: 'Bridge token is not configured.'
        });
        return;
      }

      const headers = { Accept: 'application/json' };
      let payload = null;
      if (body !== null && body !== undefined) {
        headers['Content-Type'] = 'application/json';
        payload = JSON.stringify(body);
      }
      if (token) headers['X-ACB-Token'] = token;

      try {
        GM_xmlhttpRequest({
          method,
          url: `${base}${path}`,
          headers,
          data: payload,
          timeout: Number(options.timeout) || BRIDGE_REQUEST_TIMEOUT_MS,
          responseType: 'text',
          onload(response) {
            let data = null;
            try {
              data = response.responseText ? JSON.parse(response.responseText) : null;
            } catch (_) { }

            const status = Number(response.status) || 0;
            const ok = status >= 200 && status < 300 && data?.ok !== false;
            const errorCode = String(data?.error?.code || '');
            const retriable = data?.error?.retriable === true || status >= 500 || status === 0;
            resolve({
              ok,
              status,
              data,
              errorCode,
              retriable,
              message: String(data?.error?.message || response.statusText || (ok ? 'OK' : 'Bridge request failed'))
            });
          },
          ontimeout() {
            resolve({
              ok: false,
              status: 0,
              data: null,
              errorCode: 'timeout',
              retriable: true,
              message: 'Bridge request timed out.'
            });
          },
          onerror() {
            resolve({
              ok: false,
              status: 0,
              data: null,
              errorCode: 'bridge_offline',
              retriable: true,
              message: 'Bridge is not reachable.'
            });
          },
          onabort() {
            resolve({
              ok: false,
              status: 0,
              data: null,
              errorCode: 'aborted',
              retriable: true,
              message: 'Bridge request was aborted.'
            });
          }
        });
      } catch (error) {
        resolve({
          ok: false,
          status: 0,
          data: null,
          errorCode: 'bridge_exception',
          retriable: true,
          message: error?.message || 'Bridge request could not be started.'
        });
      }
    });
  }

  function bridgeJobKey(jobId) {
    return `${BRIDGE_JOB_PREFIX}${String(jobId || '')}`;
  }

  function readBridgeJob(jobId) {
    if (!jobId) return null;
    try {
      const raw = GM_getValue(bridgeJobKey(jobId), null);
      const parsed = raw ? JSON.parse(raw) : null;
      return parsed && parsed.version === 1 && parsed.jobId === jobId ? parsed : null;
    } catch (_) {
      return null;
    }
  }


  function listBridgeJobs() {
    const jobs = [];
    try {
      const keys = typeof GM_listValues === 'function' ? GM_listValues() : [];
      for (const key of keys) {
        if (!String(key).startsWith(BRIDGE_JOB_PREFIX)) continue;
        try {
          const raw = GM_getValue(key, null);
          const job = raw ? JSON.parse(raw) : null;
          if (!job || job.version !== 1 || !job.jobId || !job.receipt) continue;
          jobs.push(job);
        } catch (_) { }
      }
    } catch (_) { }
    return jobs;
  }

  function bridgeQueueStats(conversationKey = '', jobsSnapshot = null) {
    const source = Array.isArray(jobsSnapshot) ? jobsSnapshot : listBridgeJobs();
    const jobs = source.filter(job => !conversationKey || job.conversationKey === conversationKey);
    return {
      total: jobs.length,
      pending: jobs.filter(job => !job.permanent).length,
      failed: jobs.filter(job => Boolean(job.permanent)).length,
      jobs
    };
  }

  function saveBridgeJob(job, options = {}) {
    if (!job?.jobId) return false;
    const key = bridgeJobKey(job.jobId);
    try {
      const payload = JSON.stringify(job);
      GM_setValue(key, payload);
      if (GM_getValue(key, null) !== payload) throw new Error('bridge queue read-back mismatch');
      if (options.signal !== false) signalBridgeQueueChange();
      return true;
    } catch (error) {
      setStatus(`Bridge queue persistence failed: ${error?.message || 'userscript storage failure'}. The cached audit itself was not discarded.`, 'error');
      return false;
    }
  }

  function deleteBridgeJob(jobId, options = {}) {
    if (!jobId) return false;
    try {
      GM_deleteValue(bridgeJobKey(jobId));
      if (options.signal !== false) signalBridgeQueueChange();
      return true;
    } catch (_) {
      return false;
    }
  }

  function signalBridgeQueueChange() {
    try {
      GM_setValue(BRIDGE_QUEUE_SIGNAL_KEY, `${Date.now()}:${Math.random().toString(36).slice(2, 8)}`);
    } catch (_) { }
  }

  function clearBridgeFlushTimer() {
    if (!bridgeFlushTimer) return;
    clearTimeout(bridgeFlushTimer);
    bridgeFlushTimer = 0;
  }

  function bridgeBackoffMs(attempts) {
    const index = Math.max(0, Math.min(BRIDGE_RETRY_DELAYS_MS.length - 1, Number(attempts || 0) - 1));
    return BRIDGE_RETRY_DELAYS_MS[index] || BRIDGE_RETRY_DELAYS_MS[0];
  }

  function readBridgeFlushLease() {
    try {
      const raw = GM_getValue(BRIDGE_FLUSH_LEASE_KEY, null);
      return raw ? JSON.parse(raw) : null;
    } catch (_) {
      return null;
    }
  }

  function claimBridgeFlushLease() {
    const now = Date.now();
    const existing = readBridgeFlushLease();
    if (
      existing &&
      existing.ownerId &&
      existing.ownerId !== autoInstanceId &&
      Number(existing.expiresAt) > now
    ) return false;

    const nonce = `${now.toString(36)}:${Math.random().toString(36).slice(2, 10)}`;
    const lease = {
      ownerId: autoInstanceId,
      nonce,
      expiresAt: now + BRIDGE_FLUSH_LEASE_MS
    };

    try {
      GM_setValue(BRIDGE_FLUSH_LEASE_KEY, JSON.stringify(lease));
      const verified = readBridgeFlushLease();
      return Boolean(
        verified &&
        verified.ownerId === autoInstanceId &&
        verified.nonce === nonce &&
        Number(verified.expiresAt) > Date.now()
      );
    } catch (_) {
      return false;
    }
  }

  function renewBridgeFlushLease() {
    const current = readBridgeFlushLease();
    if (
      !current ||
      current.ownerId !== autoInstanceId ||
      !current.nonce ||
      Number(current.expiresAt || 0) <= Date.now()
    ) return false;

    const renewed = {
      ...current,
      expiresAt: Date.now() + BRIDGE_FLUSH_LEASE_MS
    };

    try {
      GM_setValue(BRIDGE_FLUSH_LEASE_KEY, JSON.stringify(renewed));
      const verified = readBridgeFlushLease();
      return Boolean(
        verified &&
        verified.ownerId === autoInstanceId &&
        verified.nonce === current.nonce &&
        Number(verified.expiresAt) > Date.now()
      );
    } catch (_) {
      return false;
    }
  }

  function releaseBridgeFlushLease() {
    const lease = readBridgeFlushLease();
    if (!lease || lease.ownerId !== autoInstanceId) return;
    try {
      GM_setValue(BRIDGE_FLUSH_LEASE_KEY, JSON.stringify({
        ownerId: '',
        nonce: '',
        expiresAt: 0
      }));
    } catch (_) { }
  }

  function ensureAuditRunId() {
    if (!autoRuntime) return '';
    if (autoRuntime.runId) return autoRuntime.runId;

    const created = createAuditRunId();
    autoRuntime.runId = created;
    if (!saveAutoRuntime({ pauseOnFailure: false })) {
      // A run id is lineage authority, not a best-effort decoration. Never let
      // results/bridge jobs escape under an id that reload cannot recover.
      if (autoRuntime.runId === created) autoRuntime.runId = '';
      return '';
    }
    return created;
  }

  function bridgeConversationIdFromKey(conversationKey) {
    const match = String(conversationKey || '').match(/^c:(.+)$/);
    return match ? match[1] : '';
  }

  function createBridgeReceipt(runId, kind) {
    const random = globalThis.crypto && typeof globalThis.crypto.randomUUID === 'function'
      ? globalThis.crypto.randomUUID().replace(/-/g, '').slice(0, 16)
      : `${Math.random().toString(36).slice(2, 10)}${Math.random().toString(36).slice(2, 10)}`;
    return `${kind}-${String(runId || 'run').slice(-24)}-${Date.now().toString(36)}-${random}`;
  }

  function createBridgeMaterializeReceipt(runId, kind) {
    // Keep forced-save receipts deliberately short and inside conservative
    // idempotency-key limits. A fresh delivery run_id supplies the real batch
    // identity; the receipt only has to be unique for this request.
    const random = globalThis.crypto && typeof globalThis.crypto.randomUUID === 'function'
      ? globalThis.crypto.randomUUID().replace(/-/g, '').slice(0, 10)
      : Math.random().toString(36).slice(2, 12);
    return `${kind}-m-${Date.now().toString(36)}-${random}`;
  }

  function createBridgeMaterializeRunId() {
    const random = globalThis.crypto && typeof globalThis.crypto.randomUUID === 'function'
      ? globalThis.crypto.randomUUID().replace(/-/g, '').slice(0, 12)
      : `${Math.random().toString(36).slice(2, 8)}${Math.random().toString(36).slice(2, 8)}`;
    return `acb-mat-${Date.now().toString(36)}-${random}`;
  }

  function bridgeWaveOrder(kind) {
    if (kind === 'core') return 1;
    if (kind === 'second') return 2;
    if (kind === 'performance') return 3;
    return 9;
  }

  function reconcileStagedBridgeJob(job) {
    if (!job?.staged) return job;
    const expectedRunId = String(job.sourceRunId || job.runId || '');
    const record = readAuditResultFresh(job.wave, job.conversationKey);
    const receiptMatches = Boolean(
      record &&
      String(record.runId || '') === expectedRunId &&
      (job.materialize
        ? String(record.bridgeMaterializeReceipt || '') === String(job.receipt || '')
        : String(record.bridgeReceipt || '') === String(job.receipt || ''))
    );

    if (!receiptMatches || !Number(record?.bridgeQueuedAt || record?.bridgeMaterializeQueuedAt || 0)) {
      deleteBridgeJob(job.jobId, { signal: false });
      return null;
    }

    const active = { ...job, staged: false, updatedAt: Date.now() };
    return saveBridgeJob(active, { signal: false }) ? active : null;
  }

  function enqueueBridgeAuditRecord(record, options = {}) {
    if (!record?.text || !record?.kind || !record?.conversationKey) return false;
    if (Number(record.bridgeSavedAt) > 0 && !options.force) return true;

    const runId = String(record.runId || autoRuntime?.runId || ensureAuditRunId());
    if (!runId) return false;
    const deliveryRunId = String(options.deliveryRunId || runId);
    const canonicalReceipt = String(record.bridgeReceipt || createBridgeReceipt(runId, record.kind));
    const receipt = options.freshReceipt ? createBridgeMaterializeReceipt(deliveryRunId, record.kind) : canonicalReceipt;

    const existingJob = readBridgeJob(receipt);
    if (existingJob && !options.force) {
      scheduleBridgeFlush(50);
      return true;
    }

    const now = Date.now();
    const job = {
      version: 1,
      jobId: receipt,
      receipt,
      runId,
      sourceRunId: runId,
      deliveryRunId,
      conversationKey: record.conversationKey,
      conversationId: bridgeConversationIdFromKey(record.conversationKey),
      project: record.projectName || 'PROJECT',
      wave: record.kind,
      completedAt: Number(record.completedAt) || now,
      content: String(record.text),
      attempts: 0,
      nextAttemptAt: now,
      permanent: false,
      errorCode: '',
      lastError: '',
      materialize: Boolean(options.freshReceipt),
      staged: true,
      inFlightAt: 0,
      deliveredAwaitingAck: false,
      deliveredData: null,
      createdAt: now,
      updatedAt: now
    };

    // Persist the actual retry authority before exposing queued metadata. Signal
    // is suppressed until the matching audit record is committed, preventing a
    // cross-tab flush from racing this local two-record transaction.
    if (!saveBridgeJob(job, { signal: false })) return false;

    const queuedRecord = patchAuditResult(record.kind, next => {
      next.runId = runId;
      if (!next.bridgeReceipt) next.bridgeReceipt = canonicalReceipt;
      next.bridgeQueuedAt = now;
      next.bridgeError = '';
      if (options.freshReceipt) {
        next.bridgeMaterializeReceipt = receipt;
        next.bridgeMaterializeRunId = deliveryRunId;
        next.bridgeMaterializeQueuedAt = now;
        next.bridgeSavedAt = 0;
        next.bridgeFiles = [];
        next.savedAt = 0;
        next.savedFileName = '';
        next.saveError = '';
        if (record.kind === 'performance') {
          next.combinedSavedAt = 0;
          next.combinedFileName = '';
        }
      } else {
        next.bridgeSavedAt = Number(next.bridgeSavedAt) || 0;
      }
    }, record.conversationKey, { expectedRunId: runId });

    if (!queuedRecord) {
      deleteBridgeJob(job.jobId, { signal: false });
      signalBridgeQueueChange();
      return false;
    }

    // Phase 2: expose the job to delivery only after queued metadata exists. If
    // this write fails, the staged job remains durable and flush-time
    // reconciliation can safely complete or discard it without remote I/O.
    saveBridgeJob({ ...job, staged: false, updatedAt: Date.now() }, { signal: false });
    signalBridgeQueueChange();
    if (options.deferFlush) {
      renderAutoAuditState();
      return true;
    }
    if (state?.autoSaveAuditFiles && state?.bridgeEnabled) {
      flushBridgeQueue({ force: true, conversationKey: record.conversationKey })
        .catch(() => scheduleBridgeFlush(50));
    } else {
      scheduleBridgeFlush(50);
    }
    renderAutoAuditState();
    return true;
  }

  function bridgeJobRequest(job) {
    return {
      api_version: BRIDGE_API_VERSION,
      receipt: job.receipt,
      run_id: job.deliveryRunId || job.runId,
      conversation_id: job.conversationId || '',
      project_id: job.projectId || '',
      project_name: job.project,
      project: job.project,
      wave: job.wave,
      status: 'complete',
      completed_at: new Date(job.completedAt || Date.now()).toISOString(),
      content: job.content
    };
  }

  function markBridgeJobPermanent(job, response) {
    const latest = readBridgeJob(job?.jobId || job?.receipt || '');
    if (!latest) return false;
    const errorCode = response.errorCode || `http_${response.status || 0}`;
    const canonical = readAuditResultFresh(latest.wave, latest.conversationKey);
    const canCompact =
      errorCode !== 'invalid_auth' &&
      canonical &&
      String(canonical.runId || '') === String(latest.sourceRunId || latest.runId || '') &&
      Boolean(canonical.text);
    const next = {
      ...latest,
      permanent: true,
      errorCode,
      lastError: response.message || 'Permanent bridge error.',
      updatedAt: Date.now(),
      nextAttemptAt: 0,
      inFlightAt: 0,
      ...(canCompact ? { content: '', contentOmitted: true } : {})
    };
    if (!saveBridgeJob(next)) return false;
    patchAuditResult(latest.wave, record => {
      record.bridgeError = next.lastError;
      record.saveError = next.lastError;
    }, latest.conversationKey, { expectedRunId: String(latest.sourceRunId || latest.runId || '') });
    return true;
  }

  function markBridgeJobRetry(job, response) {
    const latest = readBridgeJob(job?.jobId || job?.receipt || '');
    if (!latest) return false;
    const attempts = Math.max(0, Number(latest.attempts) || 0) + 1;
    const next = {
      ...latest,
      attempts,
      permanent: false,
      errorCode: response.errorCode || `http_${response.status || 0}`,
      lastError: response.message || 'Transient bridge error.',
      updatedAt: Date.now(),
      nextAttemptAt: Date.now() + bridgeBackoffMs(attempts),
      inFlightAt: 0
    };
    if (!saveBridgeJob(next)) return false;
    patchAuditResult(latest.wave, record => {
      record.bridgeError = next.lastError;
      record.saveError = '';
    }, latest.conversationKey, { expectedRunId: String(latest.sourceRunId || latest.runId || '') });
    return true;
  }

  function markBridgeJobSaved(job, response) {
    const files = Array.isArray(response.data?.files) ? response.data.files.map(value => String(value)) : [];
    const expectedRunId = String(job.sourceRunId || job.runId || '');
    const patched = patchAuditResult(job.wave, record => {
      const now = Date.now();
      record.bridgeSavedAt = now;
      record.bridgeFiles = files;
      record.bridgeError = '';
      record.savedAt = now;
      record.savedFileName = files[0] || `${job.project} via AUDAPACK Bridge`;
      record.saveError = '';
      if (job.materialize) {
        record.bridgeMaterializedAt = now;
        record.bridgeMaterializeReceipt = job.receipt;
      }
      if (job.wave === 'performance' && response.data?.all3_ready) {
        record.combinedSavedAt = now;
        const all3 = files.find(value => /ALL_3/i.test(value));
        if (all3) record.combinedFileName = all3;
      }
    }, job.conversationKey, { expectedRunId });

    if (patched) {
      deleteBridgeJob(job.jobId);
      return true;
    }

    const currentRecord = readAuditResultFresh(job.wave, job.conversationKey);
    const storedRuntime = readStoredRuntime(job.conversationKey);
    const clearlyStale =
      (currentRecord && String(currentRecord.runId || '') && String(currentRecord.runId || '') !== expectedRunId) ||
      storedRuntime.runtime?.resetBarrierActive ||
      (storedRuntime.runtime?.runId && String(storedRuntime.runtime.runId) !== expectedRunId);
    if (clearlyStale) {
      deleteBridgeJob(job.jobId);
      return true;
    }

    // Remote write succeeded, local acknowledgement did not. Keep a compact
    // local-ack job so retry never POSTs the same materialization again.
    const latest = readBridgeJob(job.jobId) || job;
    return saveBridgeJob({
      ...latest,
      deliveredAwaitingAck: true,
      deliveredData: response.data || {},
      inFlightAt: 0,
      nextAttemptAt: Date.now() + 1000,
      lastError: 'Remote bridge save succeeded; waiting for local audit-result acknowledgement.',
      updatedAt: Date.now()
    });
  }

  function bridgeRouteSuffix(info) {
    const group = String(info?.group || '').trim();
    if (!group) return '';
    return ` · ${group}${Number(info?.slot) || ''}`;
  }

  async function deliverBridgeJob(job) {
    if (job.deliveredAwaitingAck) {
      return markBridgeJobSaved(job, { ok: true, data: job.deliveredData || {} });
    }

    const activeJob = {
      ...job,
      inFlightAt: Date.now(),
      updatedAt: Date.now()
    };

    // Registry handshake: resolve the stable project identity + logical route
    // before delivery so the payload carries a canonical project_id and the
    // UI can show where the audit will land. Never store physical paths here.
    if (!activeJob.projectId && activeJob.project) {
      try {
        const resolveRes = await bridgeRequest(
          'POST',
          '/v1/projects/resolve',
          { project_name: activeJob.project },
          { timeout: 7000 }
        );
        if (resolveRes.ok && resolveRes.data?.ok) {
          activeJob.projectId = String(resolveRes.data.project_id || '');
          activeJob.projectGroup = String(resolveRes.data.group || '');
          activeJob.projectSlot = Number(resolveRes.data.slot || 0);
          activeJob.registryRevision = Number(resolveRes.data.registry_revision || 0);
          activeJob.projectResolutionStatus = 'RESOLVED';
        } else if (resolveRes.retriable || !resolveRes.status) {
          activeJob.projectResolutionStatus = 'WAITING FOR BRIDGE';
        } else {
          activeJob.projectResolutionStatus = 'ERROR';
        }
      } catch (_) {
        activeJob.projectResolutionStatus = 'WAITING FOR BRIDGE';
      }
      const persisted = readBridgeJob(activeJob.jobId || '');
      saveBridgeJob({
        ...(persisted || activeJob),
        projectId: activeJob.projectId || '',
        projectGroup: activeJob.projectGroup || '',
        projectSlot: activeJob.projectSlot || 0,
        registryRevision: activeJob.registryRevision || 0,
        projectResolutionStatus: activeJob.projectResolutionStatus,
        inFlightAt: activeJob.inFlightAt,
        updatedAt: Date.now()
      }, { signal: false });
      renderAutoAuditState();
    }

    if (!saveBridgeJob(activeJob, { signal: false })) return false;

    const response = await bridgeRequest('POST', '/v1/audits', bridgeJobRequest(activeJob));
    if (response.ok && (!response.data || typeof response.data !== 'object')) {
      bridgeState = 'offline';
      bridgeMessage = 'Bridge returned an invalid success payload; audit remains queued.';
      markBridgeJobRetry(activeJob, { ...response, errorCode: 'invalid_success_payload', retriable: true, message: 'Bridge returned HTTP success without a valid JSON acknowledgement.' });
      return false;
    }

    if (response.ok && activeJob.materialize && response.data?.duplicate) {
      if (!readBridgeJob(activeJob.jobId || activeJob.receipt)) {
        bridgeState = 'connected';
        bridgeMessage = 'Connected. Forced SAVE was completed by another ACB tab.';
        bridgeLastCheckedAt = Date.now();
        return true;
      }
      bridgeState = 'error';
      bridgeMessage = 'Bridge returned duplicate for a forced physical rewrite.';
      markBridgeJobPermanent(activeJob, { ...response, errorCode: 'materialize_duplicate_unverified', message: 'Forced SAVE received duplicate acknowledgement instead of a verified physical rewrite.' });
      return false;
    }

    if (response.ok && activeJob.materialize) {
      const files = Array.isArray(response.data?.files) ? response.data.files.filter(Boolean) : [];
      if (!files.length) {
        bridgeState = 'error';
        bridgeMessage = 'Bridge did not return written file paths for forced SAVE.';
        markBridgeJobPermanent(activeJob, { ...response, errorCode: 'materialize_files_unverified', message: 'Forced SAVE returned success without written file paths, so physical materialization cannot be verified.' });
        return false;
      }
    }

    if (response.ok) {
      const files = Array.isArray(response.data?.files) ? response.data.files.filter(Boolean) : [];
      if (!response.data?.duplicate && !files.length) {
        bridgeState = 'offline';
        bridgeMessage = 'Bridge success did not identify a written audit file; job remains queued.';
        markBridgeJobRetry(activeJob, { ...response, errorCode: 'success_files_unverified', retriable: true, message: 'Bridge returned success without written file paths.' });
        return false;
      }
      bridgeState = 'connected';
      bridgeMessage = (response.data?.duplicate
        ? 'Connected. Duplicate-safe audit receipt acknowledged.'
        : 'Connected. Audit persistence active.') + bridgeRouteSuffix(response.data);
      bridgeLastCheckedAt = Date.now();
      return markBridgeJobSaved(activeJob, response);
    }

    if (response.status === 401 || response.status === 403 || response.errorCode === 'invalid_auth') {
      bridgeState = 'auth';
      bridgeMessage = 'Bridge is reachable, but the token is invalid or missing.';
      markBridgeJobPermanent(activeJob, { ...response, errorCode: 'invalid_auth', retriable: false });
      return false;
    }
    if (response.status === 409 || response.errorCode === 'receipt_conflict') {
      bridgeState = 'error';
      bridgeMessage = 'Bridge rejected an idempotency receipt conflict.';
      markBridgeJobPermanent(activeJob, { ...response, retriable: false });
      return false;
    }
    if (response.retriable) {
      bridgeState = 'offline';
      bridgeMessage = response.message || 'Bridge/output is temporarily unavailable; queued saves will retry.';
      markBridgeJobRetry(activeJob, response);
      return false;
    }
    bridgeState = 'error';
    bridgeMessage = response.message || 'Bridge rejected the audit payload.';
    markBridgeJobPermanent(activeJob, response);
    return false;
  }

  function nextBridgeFlushDelay(jobsSnapshot = null) {
    const source = Array.isArray(jobsSnapshot) ? jobsSnapshot : listBridgeJobs();
    const jobs = source.filter(job => !job.permanent);
    if (!jobs.length) return 0;
    const soonest = Math.min(...jobs.map(job => Math.max(0, Number(job.nextAttemptAt) || 0)));
    return Math.max(50, soonest - Date.now());
  }

  function scheduleBridgeFlush(delay = null, jobsSnapshot = null) {
    if (!state?.bridgeEnabled || !state?.autoSaveAuditFiles) return;
    clearBridgeFlushTimer();
    const jobs = Array.isArray(jobsSnapshot) ? jobsSnapshot : listBridgeJobs();
    if (!jobs.some(job => !job.permanent)) return;
    const computed = delay === null ? nextBridgeFlushDelay(jobs) : Number(delay);
    if (!Number.isFinite(computed) || computed < 0) return;
    bridgeFlushTimer = setTimeout(() => {
      bridgeFlushTimer = 0;
      flushBridgeQueue().catch(() => { });
    }, Math.max(0, computed));
  }

  async function flushBridgeQueue(options = {}) {
    if (
      bridgeFlushInFlight ||
      !state?.bridgeEnabled ||
      (!state?.autoSaveAuditFiles && !options.manual)
    ) return false;
    if (!normalizedBridgeUrl()) {
      bridgeState = 'error';
      bridgeMessage = 'Bridge URL is invalid. Only loopback HTTP is allowed.';
      renderAutoAuditState();
      return false;
    }

    if (!bridgeToken()) {
      bridgeState = 'auth';
      bridgeMessage = 'Paste the bridge token once in Settings.';
      renderAutoAuditState();
      return false;
    }

    if (!claimBridgeFlushLease()) {
      scheduleBridgeFlush(1200);
      return false;
    }

    bridgeFlushInFlight = true;
    try {
      const now = Date.now();
      const requestedConversation = String(options.conversationKey || '');
      const jobs = listBridgeJobs()
        .map(job => reconcileStagedBridgeJob(job))
        .filter(Boolean)
        .filter(job =>
          !job.staged &&
          !job.permanent &&
          (!requestedConversation || job.conversationKey === requestedConversation) &&
          (options.force || Number(job.nextAttemptAt || 0) <= now)
        )
        .sort((a, b) => {
          const timeDelta = Number(a.createdAt || 0) - Number(b.createdAt || 0);
          if (timeDelta) return timeDelta;
          return bridgeWaveOrder(a.wave) - bridgeWaveOrder(b.wave);
        });

      for (const job of jobs) {
        // Long backlogs can outlive the original flush lease. Renew before each
        // irreversible delivery so a standby tab cannot start the same job set
        // while this owner is still actively draining it.
        if (!renewBridgeFlushLease()) {
          bridgeMessage = 'Bridge flush ownership changed; remaining jobs stay queued for the current owner.';
          break;
        }

        // Re-read before delivery: another tab may already have completed it.
        let current = null;
        try {
          const raw = GM_getValue(bridgeJobKey(job.jobId), null);
          current = raw ? JSON.parse(raw) : null;
        } catch (_) { }
        if (!current || current.permanent) continue;

        await deliverBridgeJob(current);
      }
    } finally {
      bridgeFlushInFlight = false;
      releaseBridgeFlushLease();
      renderAutoAuditState();
      scheduleBridgeFlush();
    }
    return true;
  }

  async function flushBridgeQueueManualReliable(conversationKey, options = {}) {
    const maxAttempts = Math.max(1, Number(options.maxAttempts) || 5);
    const delays = [0, 180, 420, 850, 1400];

    for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
      const statsBefore = bridgeQueueStats(conversationKey);
      if (!statsBefore.pending) return true;

      if (attempt > 0) {
        await sleep(delays[Math.min(attempt, delays.length - 1)]);
      }

      const flushed = await flushBridgeQueue({
        force: true,
        manual: true,
        conversationKey
      });

      const statsAfter = bridgeQueueStats(conversationKey);
      if (!statsAfter.pending) return statsAfter.failed === 0;

      // false commonly means another tab owns the short bridge flush lease or
      // this tab still has a prior flush finishing. Do not steal ownership;
      // simply retry for a bounded period.
      if (flushed && statsAfter.failed > 0) return false;
    }

    signalBridgeQueueChange();
    scheduleBridgeFlush(100);
    return bridgeQueueStats(conversationKey).pending === 0;
  }

  function resetBridgeFailedJobs(errorCode = '') {
    let changed = 0;
    for (const job of listBridgeJobs()) {
      if (!job.permanent) continue;
      if (errorCode && job.errorCode !== errorCode) continue;
      const next = {
        ...job,
        permanent: false,
        attempts: 0,
        errorCode: '',
        lastError: '',
        nextAttemptAt: Date.now(),
        inFlightAt: 0,
        updatedAt: Date.now()
      };
      if (saveBridgeJob(next)) changed += 1;
    }
    if (changed) scheduleBridgeFlush(50);
    return changed;
  }

  async function checkBridge(options = {}) {
    if (!state?.bridgeEnabled) {
      bridgeState = 'disabled';
      bridgeMessage = 'Bridge integration is disabled.';
      renderAutoAuditState();
      return false;
    }

    const url = normalizedBridgeUrl();
    if (!url) {
      bridgeState = 'error';
      bridgeMessage = 'Invalid URL. Use http://127.0.0.1:<port> or http://localhost:<port>.';
      renderAutoAuditState();
      return false;
    }

    bridgeState = 'checking';
    bridgeMessage = 'Checking localhost bridge...';
    renderBridgeState();

    const health = await bridgeRequest('GET', '/health', null, { auth: false, timeout: 5000 });
    if (!health.ok) {
      bridgeState = 'offline';
      bridgeMessage = health.message || 'Bridge is offline.';
      bridgeLastCheckedAt = Date.now();
      renderAutoAuditState();
      return false;
    }

    // Identity handshake: HTTP success alone never proves AUDAPACK ownership.
    const healthService = String(health.data?.service || '').trim();
    if (healthService && healthService !== 'AUDAPACK Bridge') {
      bridgeState = 'wrong_service';
      bridgeMessage = healthService === 'ACBBridge'
        ? 'A legacy ACBBridge is answering on this port. Stop it or switch ports.'
        : `Wrong service on this port: ${healthService}.`;
      bridgeLastCheckedAt = Date.now();
      renderAutoAuditState();
      return false;
    }
    if (!healthService) {
      bridgeState = 'wrong_service';
      bridgeMessage = 'Unidentified service on this port (no service identity in /health).';
      bridgeLastCheckedAt = Date.now();
      renderAutoAuditState();
      return false;
    }
    const healthApi = Number(health.data?.api_version || 0);
    if (healthApi && healthApi !== BRIDGE_API_VERSION) {
      bridgeState = 'api_incompatible';
      bridgeMessage = `Bridge speaks API v${healthApi}; this widget requires v${BRIDGE_API_VERSION}. Update the other side.`;
      bridgeLastCheckedAt = Date.now();
      renderAutoAuditState();
      return false;
    }

    if (!bridgeToken()) {
      bridgeState = 'auth';
      bridgeMessage = 'Bridge is running. Paste/save the token to enable audit writes.';
      bridgeLastCheckedAt = Date.now();
      renderAutoAuditState();
      return false;
    }

    const status = await bridgeRequest('GET', '/v1/status', null, { timeout: 7000 });
    bridgeLastCheckedAt = Date.now();

    if (!status.ok || !status.data || typeof status.data !== 'object') {
      bridgeState = status.status === 401 || status.status === 403
        ? 'auth'
        : (status.retriable ? 'offline' : 'error');
      bridgeMessage = !status.ok
        ? (status.message || 'Bridge status check failed.')
        : 'Bridge /v1/status returned HTTP success without a valid JSON status payload.';
      renderAutoAuditState();
      return false;
    }

    bridgeState = 'connected';
    bridgeServerVersion = String(status.data?.version || status.data?.bridge_version || '');
    bridgeOutputRoot = String(
      status.data?.output_root ||
      status.data?.output?.root ||
      status.data?.output ||
      ''
    );
    bridgeMessage = bridgeOutputRoot
      ? `Connected · ${bridgeOutputRoot}`
      : 'Connected · automatic audit persistence ready.';
    renderAutoAuditState();

    resetBridgeFailedJobs('invalid_auth');

    if (!options.suppressFlush) {
      scheduleBridgeFlush(options.force ? 0 : 50);
    }

    return true;
  }

  function renderBridgeState(jobsSnapshot = null) {
    const node = panel?.querySelector('#acb-bridge-state');
    if (!node) return;
    const stats = bridgeQueueStats('', jobsSnapshot);
    const tokenPresent = Boolean(bridgeToken());
    const labels = { unknown: 'UNKNOWN', disabled: 'OFF', checking: 'CHECK', connected: 'CONNECTED', offline: 'OFFLINE', auth: 'TOKEN', error: 'ERROR', wrong_service: 'WRONG SERVICE', api_incompatible: 'API INCOMPATIBLE' };
    node.dataset.state = bridgeState === 'connected' ? 'ready' : bridgeState === 'checking' || bridgeState === 'unknown' ? 'warning' : 'error';
    node.textContent = `${labels[bridgeState] || 'UNKNOWN'} · queued ${stats.pending} · failed ${stats.failed}`;
    node.title = `${bridgeMessage}${tokenPresent ? ' Token stored.' : ' No token stored.'}${bridgeServerVersion ? ` Bridge ${bridgeServerVersion}.` : ''}${bridgeOutputRoot ? ` Output: ${bridgeOutputRoot}` : ''} Browser-fallback ALL_3 uses compact_v1. Server-generated __00_AUDIT_ALL_3.md also requires the included ACBBridge 1.0.1 compact patch.`;
  }

  function currentBridgeSaveState(conversationKey = autoBoundConversationKey || currentConversationKey(), jobsSnapshot = null) {
    if (!state?.autoSaveAuditFiles || !state?.bridgeEnabled) return { pending: 0, failed: 0 };
    return bridgeQueueStats(conversationKey, jobsSnapshot);
  }

  function installBridgeQueueListener() {
    if (bridgeQueueListenerId !== null || typeof GM_addValueChangeListener !== 'function') return;
    try {
      bridgeQueueListenerId = GM_addValueChangeListener(BRIDGE_QUEUE_SIGNAL_KEY, () => {
        renderAutoAuditState();
        if (state?.bridgeEnabled && state?.autoSaveAuditFiles) scheduleBridgeFlush(100);
      });
    } catch (_) {
      bridgeQueueListenerId = null;
    }
  }

  function openAuditFsDb() {
    return new Promise((resolve, reject) => {
      if (!globalThis.indexedDB) {
        reject(new Error('IndexedDB is unavailable'));
        return;
      }
      const request = indexedDB.open(AUDIT_FS_DB_NAME, AUDIT_FS_DB_VERSION);
      request.onupgradeneeded = () => {
        const db = request.result;
        if (!db.objectStoreNames.contains(AUDIT_FS_STORE)) db.createObjectStore(AUDIT_FS_STORE);
      };
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error || new Error('IndexedDB open failed'));
    });
  }

  async function storeAuditDirectoryHandle(handle) {
    const db = await openAuditFsDb();
    try {
      await new Promise((resolve, reject) => {
        const tx = db.transaction(AUDIT_FS_STORE, 'readwrite');
        tx.objectStore(AUDIT_FS_STORE).put(handle, AUDIT_FS_HANDLE_KEY);
        tx.oncomplete = () => resolve();
        tx.onerror = () => reject(tx.error || new Error('Directory handle save failed'));
        tx.onabort = () => reject(tx.error || new Error('Directory handle save aborted'));
      });
    } finally {
      db.close();
    }
  }

  async function loadAuditDirectoryHandle() {
    if (auditDirectoryHandle) return auditDirectoryHandle;
    try {
      const db = await openAuditFsDb();
      try {
        auditDirectoryHandle = await new Promise((resolve, reject) => {
          const tx = db.transaction(AUDIT_FS_STORE, 'readonly');
          const request = tx.objectStore(AUDIT_FS_STORE).get(AUDIT_FS_HANDLE_KEY);
          request.onsuccess = () => resolve(request.result || null);
          request.onerror = () => reject(request.error || new Error('Directory handle read failed'));
        });
      } finally {
        db.close();
      }
      return auditDirectoryHandle;
    } catch (_) {
      return null;
    }
  }

  async function auditDirectoryPermission(handle, request = false) {
    if (!handle) return 'missing';
    const options = { mode: 'readwrite' };
    try {
      if (typeof handle.queryPermission === 'function') {
        const current = await handle.queryPermission(options);
        if (current === 'granted') return 'granted';
        if (current === 'denied') return 'denied';
      }
      if (request && typeof handle.requestPermission === 'function') {
        return await handle.requestPermission(options);
      }
    } catch (_) { }
    return 'prompt';
  }

  function renderAuditFolderState() {
    const node = panel?.querySelector('#acb-audit-folder-state');
    if (!node) return;
    const handleName = auditDirectoryHandle?.name ? ` ${auditDirectoryHandle.name}` : '';
    node.dataset.state = auditDirectoryState === 'ready' ? 'ready'
      : auditDirectoryState === 'error' ? 'error'
        : 'warning';
    node.textContent = `${auditDirectoryState === 'ready' ? 'Fallback ready:' : auditDirectoryState === 'saving' ? 'Fallback saving:' : 'Fallback:'}${handleName || ' not linked'} · ${auditDirectoryMessage}`;
    node.title = `Preferred destination: ${AUDIT_OUTPUT_HINT}`;
  }

  async function refreshAuditDirectoryState() {
    const handle = await loadAuditDirectoryHandle();
    if (!handle) {
      auditDirectoryState = 'missing';
      auditDirectoryMessage = 'Choose the AUDITING_IMPLEMENTATION folder once; completed audits stay cached meanwhile.';
      renderAuditFolderState();
      renderAutoAuditState();
      return false;
    }
    const permission = await auditDirectoryPermission(handle, false);
    if (permission === 'granted') {
      auditDirectoryState = 'ready';
      auditDirectoryMessage = 'automatic Core / Second / Performance / ALL_3 writes enabled.';
      renderAuditFolderState();
      renderAutoAuditState();
      if (state?.autoSaveAuditFiles) {
        setTimeout(() => { flushCurrentAuditResultsToFolder().catch(() => { }); }, 0);
      }
      return true;
    }
    auditDirectoryState = permission === 'denied' ? 'error' : 'permission';
    auditDirectoryMessage = permission === 'denied'
      ? 'write permission denied; choose the folder again.'
      : 'write permission needs one user gesture; press Choose folder.';
    renderAuditFolderState();
    renderAutoAuditState();
    return false;
  }

  async function chooseAuditOutputFolder() {
    const picker = globalThis.showDirectoryPicker || window.showDirectoryPicker;
    if (typeof picker !== 'function') {
      auditDirectoryState = 'error';
      auditDirectoryMessage = 'this browser does not expose the File System Access directory picker.';
      renderAuditFolderState();
      setStatus('Direct folder saving is unavailable in this browser. Completed audits are still cached and copyable from 1 / 2 / 3.', 'warning');
      return false;
    }

    try {
      const handle = await picker.call(window, { id: 'ai-chatbuttons-audit-output', mode: 'readwrite' });
      const permission = await auditDirectoryPermission(handle, true);
      if (permission !== 'granted') throw new Error('write permission was not granted');
      await storeAuditDirectoryHandle(handle);
      auditDirectoryHandle = handle;
      auditDirectoryState = 'ready';
      auditDirectoryMessage = 'automatic audit file saving enabled.';
      renderAuditFolderState();
      setStatus(`Audit output folder linked: ${handle.name}. Existing completed waves in this chat will be flushed now.`, 'success');
      await flushCurrentAuditResultsToFolder({ force: true });
      return true;
    } catch (error) {
      if (error?.name === 'AbortError') {
        setStatus('Audit folder selection canceled. Nothing changed.', 'info');
        return false;
      }
      auditDirectoryState = 'error';
      auditDirectoryMessage = error?.message || 'folder selection failed.';
      renderAuditFolderState();
      setStatus(`Audit folder could not be linked: ${auditDirectoryMessage}`, 'error');
      return false;
    }
  }

  async function writeTextFileToAuditFolder(filename, content) {
    const handle = await loadAuditDirectoryHandle();
    if (!handle) throw new Error('audit output folder is not linked');
    const permission = await auditDirectoryPermission(handle, false);
    if (permission !== 'granted') throw new Error('audit output folder permission is not currently granted');
    const fileHandle = await handle.getFileHandle(filename, { create: true });
    const writable = await fileHandle.createWritable();
    try {
      await writable.write(String(content || ''));
    } finally {
      await writable.close();
    }
    return true;
  }

  async function autoSaveAuditRecord(record, options = {}) {
    if ((!state?.autoSaveAuditFiles && !options.force) || !record?.text) return false;
    if (record.savedAt && !options.force && !record.saveError) return true;
    try {
      auditDirectoryState = 'saving';
      auditDirectoryMessage = `writing ${auditResultFilename(record)}...`;
      renderAuditFolderState();
      await writeTextFileToAuditFolder(auditResultFilename(record), record.text);
      patchAuditResult(record.kind, next => {
        next.savedAt = Date.now();
        next.savedFileName = auditResultFilename(record);
        next.saveError = '';
      }, record.conversationKey, { expectedRunId: String(record.runId || '') });
      auditDirectoryState = 'ready';
      auditDirectoryMessage = 'automatic audit file saving enabled.';
      renderAuditFolderState();
      renderAutoAuditState();
      return true;
    } catch (error) {
      patchAuditResult(record.kind, next => { next.saveError = error?.message || 'save failed'; }, record.conversationKey, { expectedRunId: String(record.runId || '') });
      auditDirectoryState = /not linked|permission/i.test(String(error?.message || '')) ? 'permission' : 'error';
      auditDirectoryMessage = error?.message || 'automatic save failed.';
      renderAuditFolderState();
      renderAutoAuditState();
      return false;
    }
  }

  function auditTestMetadata(text) {
    const header = handoffHeader(normalizeAuditResponseText(text || ''));
    const status = String(header.match(/^\s*TEST_STATUS\s*:\s*(.+)$/im)?.[1] || '').trim();
    const limitation = String(header.match(/^\s*TEST_LIMITATION\s*:\s*(.+)$/im)?.[1] || '').trim();
    const verifiedInstead = String(header.match(/^\s*VERIFIED_INSTEAD\s*:\s*(.+)$/im)?.[1] || '').trim();

    const allowed = new Set([
      'TEST_PASSED',
      'TEST_FAILED',
      'TEST_PARTIAL',
      'TEST_NOT_RUN_ENVIRONMENT',
      'TEST_NOT_APPLICABLE'
    ]);

    return {
      status: allowed.has(status.toUpperCase()) ? status.toUpperCase() : '',
      limitation,
      verifiedInstead,
      complete: Boolean(
        allowed.has(status.toUpperCase()) &&
        limitation &&
        verifiedInstead
      )
    };
  }

  function auditStageFromKind(kind) {
    if (kind === 'core') return 'wait-core';
    if (kind === 'second') return 'wait-second';
    if (kind === 'performance') return 'wait-performance';
    return '';
  }

  function parseAuditHandoffParts(record) {
    const text = normalizeAuditResponseText(record?.text || '');
    const stage = auditStageFromKind(record?.kind);
    const integrity = auditHandoffIntegrity(stage, text);
    if (!integrity.valid) {
      throw new Error(`${auditWaveTitle(record?.kind)} is not structurally complete: ${integrity.reason}`);
    }

    const lines = text.split('\n');
    let firstTicketLine = lines.findIndex(line =>
      /^\s*\[P[012]\]\s*\[(?:CORE|W2|PERF)-\d{3}\]/i.test(line)
    );
    if (firstTicketLine < 0) {
      firstTicketLine = lines.findIndex(line =>
        /^\s*NO\s+(?:VERIFIED\s+CORE|NEW\s+VERIFIED\s+SECOND[-\s]*WAVE|MATERIAL\s+PERFORMANCE\s*\/\s*STABILITY)/i.test(line)
      );
    }
    if (firstTicketLine < 0) {
      throw new Error(`${auditWaveTitle(record?.kind)} has no ticket/body boundary.`);
    }

    const headerLines = lines.slice(0, firstTicketLine);
    const bodyLines = lines.slice(firstTicketLine);
    const fields = new Map();

    for (const line of headerLines) {
      const match = line.match(/^\s*([A-Z][A-Z0-9_ /-]*)\s*:\s*(.*)$/);
      if (!match) continue;
      fields.set(match[1].trim().toUpperCase(), match[2].trim());
    }

    return {
      record,
      fields,
      body: bodyLines.join('\n').trim(),
      tickets: Number(fields.get('TICKETS') || integrity.declared || 0),
      integrity
    };
  }

  function sameMetadataValue(left, right) {
    const normalize = value => normalizeAuditResponseText(value || '')
      .replace(/\s+/g, ' ')
      .trim()
      .toLowerCase();
    return normalize(left) === normalize(right);
  }

  function buildCompactAll3Text(records) {
    const filtered = records.filter(Boolean);
    if (filtered.length !== 3) {
      throw new Error('Compact ALL_3 requires Core, Second Wave and Performance.');
    }

    const parts = filtered.map(parseAuditHandoffParts);
    const core = parts[0];
    const project = String(
      core.fields.get('PROJECT_NAME') ||
      filtered.find(record => record?.projectName)?.projectName ||
      'PROJECT'
    ).trim();

    for (const part of parts) {
      const candidate = String(part.fields.get('PROJECT_NAME') || project).trim();
      if (!sameMetadataValue(candidate, project)) {
        throw new Error(`Cross-wave project mismatch: ${project} vs ${candidate}`);
      }
      if (
        filtered[0]?.runId &&
        part.record?.runId &&
        filtered[0].runId !== part.record.runId
      ) {
        throw new Error('Cross-wave run_id mismatch; refusing to merge unrelated audit runs.');
      }
    }

    const common = {
      TARGET: core.fields.get('TARGET') || '',
      BASELINE: core.fields.get('BASELINE') || '',
      GIT_CONTEXT: core.fields.get('GIT_CONTEXT') || '',
      SAIPEN_CONTEXT: core.fields.get('SAIPEN_CONTEXT') || '',
      HANDOFF: core.fields.get('HANDOFF') || 'IMPLEMENTATION_AGENT'
    };

    const totalTickets = parts.reduce((sum, part) => sum + part.tickets, 0);
    const runId = filtered[0]?.runId || filtered.find(record => record?.runId)?.runId || '';

    const output = [
      `# ${project} — Audit Handoff`,
      '',
      ...(runId ? [`RUN_ID: ${runId}`] : []),
      `GENERATED_AT: ${new Date().toISOString()}`,
      `PROJECT_NAME: ${project}`,
      ...(common.TARGET ? [`TARGET: ${common.TARGET}`] : []),
      ...(common.BASELINE ? [`BASELINE: ${common.BASELINE}`] : []),
      ...(common.GIT_CONTEXT ? [`GIT_CONTEXT: ${common.GIT_CONTEXT}`] : []),
      ...(common.SAIPEN_CONTEXT ? [`SAIPEN_CONTEXT: ${common.SAIPEN_CONTEXT}`] : []),
      `HANDOFF: ${common.HANDOFF}`,
      'WAVES: 3',
      `TOTAL_TICKETS: ${totalTickets}`
    ];

    const labels = [
      ['01', 'AUDIT CORE'],
      ['02', 'AUDIT SECOND WAVE'],
      ['03', 'AUDIT PERFORMANCE / STABILITY / EFFECTIVENESS']
    ];

    for (let index = 0; index < parts.length; index += 1) {
      const part = parts[index];
      const [number, label] = labels[index];
      const fields = part.fields;

      output.push('', `## ${number} — ${label}`);
      if (fields.get('DATE_TIME')) output.push(`DATE_TIME: ${fields.get('DATE_TIME')}`);
      if (fields.get('AUDIT_SCOPE')) output.push(`AUDIT_SCOPE: ${fields.get('AUDIT_SCOPE')}`);
      if (fields.get('TEST_STATUS')) output.push(`TEST_STATUS: ${fields.get('TEST_STATUS')}`);
      if (fields.get('TEST_LIMITATION')) output.push(`TEST_LIMITATION: ${fields.get('TEST_LIMITATION')}`);
      if (fields.get('VERIFIED_INSTEAD')) output.push(`VERIFIED_INSTEAD: ${fields.get('VERIFIED_INSTEAD')}`);
      if (fields.get('STATUS')) output.push(`STATUS: ${fields.get('STATUS')}`);
      output.push(`TICKETS: ${part.tickets}`);

      const deltas = [];
      for (const key of ['TARGET', 'BASELINE', 'GIT_CONTEXT', 'SAIPEN_CONTEXT']) {
        const value = fields.get(key) || '';
        if (value && !sameMetadataValue(value, common[key])) {
          deltas.push(`${key}: ${value}`);
        }
      }

      const coreBaseline = fields.get('CORE_BASELINE') || '';
      if (coreBaseline && !sameMetadataValue(coreBaseline, common.BASELINE)) {
        deltas.push(`CORE_BASELINE: ${coreBaseline}`);
      }

      const previousBaseline = fields.get('PREVIOUS_BASELINE') || '';
      if (previousBaseline && !sameMetadataValue(previousBaseline, common.BASELINE)) {
        deltas.push(`PREVIOUS_BASELINE: ${previousBaseline}`);
      }

      if (deltas.length) {
        output.push('CONTEXT_DELTA:');
        for (const delta of deltas) output.push(`- ${delta}`);
      }

      output.push('', part.body);
    }

    return output.join('\n').replace(/\n{4,}/g, '\n\n\n').trim() + '\n';
  }

  function buildCombinedAuditText(records) {
    return buildCompactAll3Text(records);
  }

  async function autoSaveCombinedAuditIfReady(conversationKey = autoBoundConversationKey || currentConversationKey(), options = {}) {
    if (!state?.autoSaveAuditFiles && !options.force) return false;
    const records = currentChatAuditRecords(conversationKey);
    if (records.length !== 3) return false;
    const performanceRecord = records.find(record => record.kind === 'performance');
    if (performanceRecord?.combinedSavedAt && !options.force) return true;
    const projectName = records.find(record => record.projectName)?.projectName || autoRuntime?.projectName || 'PROJECT';
    const runStartedAt = records[0]?.runStartedAt || autoRuntime?.startedAt || Date.now();
    try {
      const filename = combinedAuditFilename(projectName, runStartedAt);
      await writeTextFileToAuditFolder(filename, buildCombinedAuditText(records));
      patchAuditResult('performance', next => {
        next.combinedSavedAt = Date.now();
        next.combinedFileName = filename;
      }, conversationKey, { expectedRunId: String(performanceRecord?.runId || '') });
      auditDirectoryState = 'ready';
      auditDirectoryMessage = `saved all 3 waves + ${filename}.`;
      renderAuditFolderState();
      renderAutoAuditState();
      return true;
    } catch (error) {
      auditDirectoryState = /not linked|permission/i.test(String(error?.message || '')) ? 'permission' : 'error';
      auditDirectoryMessage = error?.message || 'combined audit save failed.';
      renderAuditFolderState();
      renderAutoAuditState();
      return false;
    }
  }

  async function flushCurrentAuditResultsToFolder(options = {}) {
    const conversationKey = autoBoundConversationKey || currentConversationKey();
    const records = currentChatAuditRecords(conversationKey);
    let saved = 0;
    for (const record of records) {
      if (await autoSaveAuditRecord(record, { force: Boolean(options.force) })) saved += 1;
    }
    const ready = records.length;
    const combined = ready === 3
      ? await autoSaveCombinedAuditIfReady(conversationKey, { force: Boolean(options.force) })
      : false;
    return { ready, saved, combined };
  }

  function currentConversationId() {
    return location.pathname.match(/^\/c\/([^/?#]+)/i)?.[1] || '';
  }

  function applyLocalConversationTitle(name) {
    const cleaned = sanitizeProjectIdentity(name);
    if (!cleaned) return false;
    try {
      document.title = cleaned;
      return true;
    } catch (_) {
      return false;
    }
  }

  function sidebarCurrentConversationLink() {
    const conversationId = currentConversationId();
    if (!conversationId) return null;
    const wanted = `/c/${conversationId}`;

    const cached = projectTitleObserverLink;
    if (cached?.isConnected) {
      try {
        const url = new URL(cached.getAttribute('href'), location.href);
        if (url.origin === location.origin && url.pathname === wanted) return cached;
      } catch (_) { }
    }

    for (const link of document.querySelectorAll('a[href]')) {
      try {
        const url = new URL(link.getAttribute('href'), location.href);
        if (url.origin !== location.origin) continue;
        if (url.pathname === wanted) return link;
      } catch (_) { }
    }
    return null;
  }


  function normalizeConversationTitle(value) {
    return cleanProjectName(String(value || '')
      .replace(/\s+/g, ' ')
      .trim());
  }

  function currentSidebarConversationTitle() {
    const link = sidebarCurrentConversationLink();
    if (!link) return '';

    const candidates = [
      String(link.getAttribute('aria-label') || '').trim(),
      String(link.getAttribute('title') || '').trim()
    ];

    for (const node of link.querySelectorAll('span, div')) {
      if (!isVisible(node)) continue;
      const value = String(node.textContent || '').replace(/\s+/g, ' ').trim();
      if (!value || value.length > 120) continue;
      candidates.push(value);
    }

    const ownText = String(link.textContent || '').replace(/\s+/g, ' ').trim();
    if (ownText && ownText.length <= 120) candidates.push(ownText);

    const noise = /^(more|options|menu|share|rename|archive|delete|chat|conversation|ещ[её]|параметр|veel)$/i;
    const cleaned = candidates
      .map(normalizeConversationTitle)
      .filter(value => value && !noise.test(value));

    // Conversation title is normally the longest meaningful text inside the
    // current sidebar link. Avoid trusting document.title because ACB itself
    // writes that optimistically before ChatGPT server state settles.
    cleaned.sort((a, b) => b.length - a.length);
    return cleaned[0] || '';
  }

  function sanitizeConversationLabel(value) {
    const cleaned = normalizeConversationTitle(value);
    if (!cleaned) return '';

    if (/^(?:CHATGPT|NEW CHAT|TEMPORARY CHAT|CHAT|PROJECT)$/i.test(cleaned)) return '';
    if (/^(?:SKIP TO (?:CONTENT|MAIN CONTENT)|VIEW CHAT|IMAGES|PLUGINS|DEEP RESEARCH|SETTINGS|HELP|LOG IN|SIGN UP|SEE PLANS AND PRICING)$/i.test(cleaned)) return '';
    if (looksOpaqueIdentity(cleaned)) return '';
    if (/^(?:https?:\/\/|blob:|data:)/i.test(cleaned)) return '';
    return cleaned.slice(0, 80);
  }


  function documentConversationLabel() {
    let value = String(document.title || '').trim();
    value = value.replace(/\s+[-–—]\s+(?:Brave|Google Chrome|Firefox|Microsoft Edge)\s*$/i, '');
    return sanitizeConversationLabel(value);
  }

  function currentMiniIdentity() {
    if (chatGPTRootIsQuarantined()) return { label: 'CHAT', kind: 'auth' };

    const project = sanitizeProjectIdentity(
      autoRuntime?.projectName || projectNameFromComposerAttachments() || ''
    );
    if (project) return { label: project, kind: 'project' };

    const chatTitle =
      sanitizeConversationLabel(currentSidebarConversationTitle()) ||
      documentConversationLabel();

    if (chatTitle) return { label: chatTitle, kind: 'chat-title' };
    return { label: 'CHAT', kind: 'chat' };
  }


  function currentConversationTitleMatches(name) {
    const wanted = normalizeConversationTitle(name);
    if (!wanted) return false;

    const sidebar = currentSidebarConversationTitle();
    if (!sidebar) return false;

    return sidebar.toLowerCase() === wanted.toLowerCase();
  }

  function renameGuardContextValid(projectName, runStartedAt, conversationKey) {
    if (!autoRuntime || !state?.autoRenameChat) return false;
    if (detectSite().key !== 'chatgpt') return false;

    const wanted = sanitizeProjectIdentity(projectName);
    if (!wanted) return false;

    const sameProject = sanitizeProjectIdentity(autoRuntime.projectName) === wanted;
    const sameRun = !runStartedAt || Number(autoRuntime.startedAt || 0) === Number(runStartedAt);
    const currentKey = currentConversationKey();
    const compatibleKey =
      !conversationKey ||
      conversationKey === currentKey ||
      (
        String(conversationKey).startsWith('draft:') &&
        currentKey.startsWith('c:') &&
        sameRun &&
        sameProject
      );

    return sameProject && sameRun && compatibleKey;
  }

  function scheduleConversationTitleGuard(projectName, options = {}) {
    const cleaned = sanitizeProjectIdentity(projectName);
    if (
      !cleaned ||
      !state?.autoRenameChat ||
      detectSite().key !== 'chatgpt' ||
      !currentConversationId() ||
      chatGPTRootIsQuarantined()
    ) {
      disconnectProjectTitleObserver();
      return;
    }

    const conversationKey = String(
      options.conversationKey ||
      autoBoundConversationKey ||
      currentConversationKey()
    );
    const runStartedAt = Number(options.runStartedAt || autoRuntime?.startedAt || 0);

    if (
      conversationTitleGuardToken &&
      conversationTitleGuardProject === cleaned &&
      conversationTitleGuardConversationKey === conversationKey &&
      Number(conversationTitleGuardRunStartedAt || 0) === Number(runStartedAt || 0) &&
      Date.now() - conversationTitleGuardStartedAt < CHAT_TITLE_GUARD_TTL_MS
    ) {
      if (renameGuardContextValid(cleaned, runStartedAt, conversationKey)) {
        applyPersistentLocalProjectTitle(cleaned);
      }
      return;
    }

    const token = `${Date.now().toString(36)}:${Math.random().toString(36).slice(2, 9)}`;
    conversationTitleGuardToken = token;
    conversationTitleGuardStartedAt = Date.now();
    conversationTitleGuardProject = cleaned;
    conversationTitleGuardConversationKey = conversationKey;
    conversationTitleGuardRunStartedAt = runStartedAt;

    if (!renameGuardContextValid(cleaned, runStartedAt, conversationKey)) return;
    applyPersistentLocalProjectTitle(cleaned);

    for (const delay of CHAT_TITLE_GUARD_DELAYS_MS) {
      setTimeout(() => {
        if (conversationTitleGuardToken !== token) return;
        if (!autoRuntime || !state?.autoRenameChat) return;

        // Route/run ownership must be checked at callback time, not only when the
        // timer was scheduled. Otherwise a delayed 15s/60s rename can wake after
        // the user moved to another chat and PATCH that unrelated conversation.
        if (!renameGuardContextValid(cleaned, runStartedAt, conversationKey)) return;

        applyPersistentLocalProjectTitle(cleaned);

        // ChatGPT may generate/replace its own title several seconds after the
        // first message. Reassert the server title only at sparse guard points;
        // rate arbitration keeps multiple ACB tabs from creating request storms.
        if (delay >= 15000 && currentConversationId()) {
          const expectedConversationId = currentConversationId();
          renameCurrentConversationViaBackend(cleaned, { force: true })
            .then(ok => {
              if (
                ok &&
                currentConversationId() === expectedConversationId &&
                renameGuardContextValid(cleaned, runStartedAt, conversationKey)
              ) {
                markConversationTitlePersisted(cleaned);
              }
            })
            .catch(() => { });
        }
      }, delay);
    }
  }

  function setNativeTextInputValue(input, value) {
    if (!input) return false;
    const proto = input instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
    if (setter) setter.call(input, value);
    else input.value = value;
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
    return true;
  }

  async function renameCurrentConversationViaUi(name) {
    // v0.0.37: intentionally disabled for automation.
    // ChatGPT's sidebar action menu is not a stable API surface: depending on
    // viewport/build it may expose Share/View files/Pin/Archive/Delete but no
    // Rename. Clicking generic "more" buttons can leave menus open or even hit
    // unrelated navigation controls. Automatic rename must never click UI.
    return false;
  }

  async function waitForVisibleConversationTitle(name) {
    const cleaned = normalizeConversationTitle(name);
    if (!cleaned) return false;

    for (const delay of CHAT_TITLE_VERIFY_DELAYS_MS) {
      if (delay) await sleep(delay);
      if (currentConversationTitleMatches(cleaned)) return true;
    }
    return false;
  }

  function markConversationTitlePersisted(name) {
    const cleaned = sanitizeProjectIdentity(name);
    if (!cleaned || !autoRuntime) return false;

    autoRuntime.renameAppliedName = cleaned;
    autoRuntime.renamePersistedName = cleaned;
    autoRuntime.renamePersistedAt = Date.now();
    autoRuntime.renameAttemptName = cleaned;
    autoRuntime.renameAttemptCount = 0;
    saveAutoRuntime({ pauseOnFailure: false });
    return true;
  }

  function readRenameRateState() {
    try {
      const raw = GM_getValue(CHAT_RENAME_RATE_STATE_KEY, null);
      const parsed = raw ? JSON.parse(raw) : null;
      if (!parsed || parsed.version !== 2) {
        return {
          version: 2,
          cooldownUntil: 0,
          globalLastAt: 0,
          lastByConversation: {}
        };
      }
      return {
        version: 2,
        cooldownUntil: Math.max(0, Number(parsed.cooldownUntil) || 0),
        globalLastAt: Math.max(0, Number(parsed.globalLastAt) || 0),
        lastByConversation: parsed.lastByConversation && typeof parsed.lastByConversation === 'object'
          ? parsed.lastByConversation
          : {}
      };
    } catch (_) {
      return {
        version: 2,
        cooldownUntil: 0,
        globalLastAt: 0,
        lastByConversation: {}
      };
    }
  }

  function writeRenameRateState(stateValue) {
    try {
      GM_setValue(CHAT_RENAME_RATE_STATE_KEY, JSON.stringify(stateValue));
      return true;
    } catch (_) {
      return false;
    }
  }

  function renameBackendCooldownRemaining() {
    const rate = readRenameRateState();
    return Math.max(0, Number(rate.cooldownUntil || 0) - Date.now());
  }

  function acquireRenameBackendSlot(conversationId) {
    if (!conversationId) return false;

    const now = Date.now();
    const rate = readRenameRateState();

    if (Number(rate.cooldownUntil || 0) > now) return false;
    if (now - Number(rate.globalLastAt || 0) < CHAT_RENAME_GLOBAL_MIN_GAP_MS) return false;

    const lastForConversation = Number(rate.lastByConversation?.[conversationId] || 0);
    if (now - lastForConversation < CHAT_RENAME_CONVERSATION_MIN_GAP_MS) return false;

    const token = `${autoTabId}:${now}:${Math.random().toString(36).slice(2, 8)}`;

    rate.globalLastAt = now;
    rate.lastByConversation = {
      ...rate.lastByConversation,
      [conversationId]: now
    };
    rate.claimToken = token;

    // Bound state size if the browser has seen many chats.
    const entries = Object.entries(rate.lastByConversation)
      .sort((a, b) => Number(b[1]) - Number(a[1]))
      .slice(0, 80);
    rate.lastByConversation = Object.fromEntries(entries);

    if (!writeRenameRateState(rate)) return false;

    // Best-effort cross-tab arbitration. GM storage is not a CAS primitive,
    // but read-back prevents most simultaneous multi-window bursts.
    const verified = readRenameRateState();
    return verified.claimToken === token;
  }

  function activateRename429CircuitBreaker(response = null) {
    const now = Date.now();
    let retryMs = CHAT_RENAME_429_COOLDOWN_MS;

    try {
      const header = String(response?.headers?.get?.('Retry-After') || '').trim();
      if (/^\d+$/.test(header)) {
        retryMs = Math.max(retryMs, Number(header) * 1000);
      } else if (header) {
        const absolute = Date.parse(header);
        if (Number.isFinite(absolute)) retryMs = Math.max(retryMs, absolute - now);
      }
    } catch (_) { }

    const rate = readRenameRateState();
    rate.cooldownUntil = Math.max(Number(rate.cooldownUntil || 0), now + Math.max(60000, retryMs));
    writeRenameRateState(rate);

    if (now - renameRateLimitNoticeAt > 60000) {
      renameRateLimitNoticeAt = now;
      setStatus(
        'Chat title rename hit ChatGPT rate limiting. Rename requests are globally suspended across ACB tabs; audits continue normally.',
        'warning'
      );
    }
  }

  async function chatGPTSessionAccessToken() {
    try {
      const response = await fetch(`${location.origin}/api/auth/session`, {
        method: 'GET',
        credentials: 'include',
        cache: 'no-store',
        headers: { Accept: 'application/json' }
      });
      if (!response.ok) return '';
      const payload = await response.json().catch(() => null);
      return String(payload?.accessToken || payload?.access_token || '');
    } catch (_) {
      return '';
    }
  }

  async function patchChatGPTConversationTitle(conversationId, title, accessToken = '') {
    const headers = {
      Accept: 'application/json',
      'Content-Type': 'application/json'
    };
    if (accessToken) headers.Authorization = `Bearer ${accessToken}`;

    return fetch(`${location.origin}/backend-api/conversation/${encodeURIComponent(conversationId)}`, {
      method: 'PATCH',
      credentials: 'include',
      cache: 'no-store',
      headers,
      body: JSON.stringify({ title })
    });
  }

  async function renameCurrentConversationViaBackend(name, options = {}) {
    if (detectSite().key !== 'chatgpt' || chatGPTRootIsQuarantined()) return false;

    const cleaned = sanitizeProjectIdentity(name);
    const conversationId = currentConversationId();
    if (!cleaned || !conversationId) return false;

    if (!options.force && autoRuntime?.renamePersistedName === cleaned && Number(autoRuntime?.renamePersistedAt) > 0) {
      return true;
    }

    if (!acquireRenameBackendSlot(conversationId)) return false;

    let response = null;
    try {
      response = await patchChatGPTConversationTitle(conversationId, cleaned);

      // Some ChatGPT builds accept the authenticated same-origin cookie request
      // directly; others require the bearer token already available to the web
      // session. Fetch it only after an auth failure and never persist/log it.
      if (response.status === 401 || response.status === 403) {
        const accessToken = await chatGPTSessionAccessToken();
        if (accessToken) {
          response = await patchChatGPTConversationTitle(conversationId, cleaned, accessToken);
        }
      }

      if (response.status === 429) {
        activateRename429CircuitBreaker(response);
        return false;
      }

      if (!response.ok) return false;

      const payload = await response.clone().json().catch(() => null);
      if (payload && payload.success === false) return false;
      return true;
    } catch (_) {
      return false;
    }
  }


  function localSidebarTitleTextNode(link = sidebarCurrentConversationLink()) {
    if (!link) return null;

    const walker = document.createTreeWalker(
      link,
      NodeFilter.SHOW_TEXT,
      {
        acceptNode(node) {
          const value = normalizeConversationTitle(node.nodeValue || '');
          if (!value || value.length > 120) return NodeFilter.FILTER_REJECT;
          const parent = node.parentElement;
          if (!parent) return NodeFilter.FILTER_REJECT;
          if (parent.closest('button, [role="button"], svg')) return NodeFilter.FILTER_REJECT;
          if (/^(more|options|menu|share|rename|archive|delete|chat|conversation)$/i.test(value)) {
            return NodeFilter.FILTER_REJECT;
          }
          return NodeFilter.FILTER_ACCEPT;
        }
      }
    );

    const nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    nodes.sort((a, b) =>
      normalizeConversationTitle(b.nodeValue || '').length -
      normalizeConversationTitle(a.nodeValue || '').length
    );
    return nodes[0] || null;
  }

  function applyLocalSidebarConversationTitle(name) {
    const cleaned = sanitizeProjectIdentity(name);
    if (!cleaned || !currentConversationId()) return false;

    const link = sidebarCurrentConversationLink();
    if (!link) return false;

    const node = localSidebarTitleTextNode(link);
    if (!node) return false;

    if (normalizeConversationTitle(node.nodeValue || '').toLowerCase() !== cleaned.toLowerCase()) {
      node.nodeValue = cleaned;
    }

    link.dataset.acbProjectTitle = cleaned;
    return true;
  }


  function disconnectProjectTitleObserver() {
    if (projectTitleRepairTimer) {
      clearTimeout(projectTitleRepairTimer);
      projectTitleRepairTimer = 0;
    }
    if (projectTitleObserver) {
      projectTitleObserver.disconnect();
      projectTitleObserver = null;
    }
    projectTitleObserverLink = null;
  }

  function scheduleProjectTitleRepair(delay = 70) {
    if (projectTitleRepairTimer) return;
    projectTitleRepairTimer = setTimeout(() => {
      projectTitleRepairTimer = 0;

      if (
        !state?.autoRenameChat ||
        !autoRuntime?.projectName ||
        !currentConversationId() ||
        chatGPTRootIsQuarantined()
      ) {
        disconnectProjectTitleObserver();
        return;
      }

      applyPersistentLocalProjectTitle(autoRuntime.projectName);
      ensureProjectTitleObserver();
    }, Math.max(20, Number(delay) || 70));
  }

  function ensureProjectTitleObserver() {
    if (
      detectSite().key !== 'chatgpt' ||
      !state?.autoRenameChat ||
      !autoRuntime?.projectName ||
      !currentConversationId() ||
      chatGPTRootIsQuarantined()
    ) {
      disconnectProjectTitleObserver();
      return false;
    }

    const link = sidebarCurrentConversationLink();
    const navRoot = link?.closest('nav, aside') || link?.parentElement || null;
    const titleNode = document.querySelector('title');

    if (
      projectTitleObserver &&
      projectTitleObserverLink === link &&
      (!navRoot || navRoot.isConnected)
    ) return true;

    disconnectProjectTitleObserver();

    projectTitleObserver = new MutationObserver(() => scheduleProjectTitleRepair(70));
    projectTitleObserverLink = link || null;

    if (titleNode) {
      projectTitleObserver.observe(titleNode, {
        childList: true,
        characterData: true,
        subtree: true
      });
    }

    if (navRoot) {
      projectTitleObserver.observe(navRoot, {
        childList: true,
        characterData: true,
        subtree: true
      });
    }

    return Boolean(titleNode || navRoot);
  }

  function applyPersistentLocalProjectTitle(name = autoRuntime?.projectName || '') {
    const cleaned = sanitizeProjectIdentity(name);
    if (!cleaned || detectSite().key !== 'chatgpt' || !currentConversationId() || chatGPTRootIsQuarantined()) return false;

    const now = Date.now();
    const elapsed = now - lastLocalTitleApplyAt;
    if (elapsed < LOCAL_TITLE_REAPPLY_MIN_MS) {
      scheduleProjectTitleRepair(Math.max(30, LOCAL_TITLE_REAPPLY_MIN_MS - elapsed + 20));
      return true;
    }

    const documentMatches = normalizeConversationTitle(document.title).toLowerCase() === cleaned.toLowerCase();
    const sidebarMatches = currentConversationTitleMatches(cleaned);
    if (sidebarMatches && documentMatches) {
      lastLocalTitleApplyAt = now;
      return true;
    }

    lastLocalTitleApplyAt = now;
    applyLocalConversationTitle(cleaned);
    applyLocalSidebarConversationTitle(cleaned);
    ensureProjectTitleObserver();
    return true;
  }


  // RENAME SAFETY INVARIANT:
  // Project naming may PATCH only the current ChatGPT conversation title.
  // It must never navigate, delete/archive, or expose authentication material.
  async function maybeRenameConversation(name, options = {}) {
    if (!state?.autoRenameChat || detectSite().key !== 'chatgpt' || !autoRuntime) return false;

    const cleaned = sanitizeProjectIdentity(name);
    if (!cleaned) return false;

    updateRuntimeProjectName(
      cleaned,
      options.source || autoRuntime.projectNameSource || 'artifact'
    );

    const requestedConversationKey = String(
      options.conversationKey || autoBoundConversationKey || currentConversationKey()
    );
    const requestedRunStartedAt = Number(options.runStartedAt || autoRuntime.startedAt || 0);
    if (!renameGuardContextValid(cleaned, requestedRunStartedAt, requestedConversationKey)) {
      return false;
    }

    // Immediate optimistic UI keeps the project identity stable while ChatGPT is
    // still creating/hydrating the server-side conversation.
    applyPersistentLocalProjectTitle(cleaned);
    scheduleConversationTitleGuard(cleaned, {
      conversationKey: options.conversationKey || autoBoundConversationKey || currentConversationKey(),
      runStartedAt: options.runStartedAt || autoRuntime.startedAt || 0
    });

    const conversationId = currentConversationId();
    if (!conversationId) {
      autoRuntime.renameAppliedName = cleaned;
      autoRuntime.renameAttemptName = cleaned;
      saveAutoRuntime({ pauseOnFailure: false });
      return true;
    }

    const sameAttempt = autoRuntime.renameAttemptName === cleaned;
    autoRuntime.renameAppliedName = cleaned;
    autoRuntime.renameAttemptName = cleaned;
    autoRuntime.renameAttemptCount = sameAttempt
      ? Math.min(CHAT_RENAME_MAX_ATTEMPTS, Number(autoRuntime.renameAttemptCount || 0) + 1)
      : 1;
    saveAutoRuntime({ pauseOnFailure: false });

    const persisted = await renameCurrentConversationViaBackend(cleaned, {
      force: Boolean(options.forceBackend)
    });

    if (persisted) {
      // The PATCH targets the conversation id captured before the await. If the
      // user navigated meanwhile, do not stamp the newly-bound runtime as though
      // its own title had been persisted by that old request.
      if (
        currentConversationId() === conversationId &&
        renameGuardContextValid(cleaned, requestedRunStartedAt, requestedConversationKey)
      ) {
        markConversationTitlePersisted(cleaned);
        applyPersistentLocalProjectTitle(cleaned);
      }
      return true;
    }

    return false;
  }

  function captureCompletedAudit(kind, text, gateState = 'complete', sourceUserId = '') {
    if (!kind || !text) return null;

    const stage = kind === 'core'
      ? 'wait-core'
      : kind === 'second'
        ? 'wait-second'
        : kind === 'performance'
          ? 'wait-performance'
          : '';

    if (gateState === 'complete') {
      const integrity = auditHandoffIntegrity(stage, text);
      if (!integrity.valid) {
        setStatus(
          `${auditWaveTitle(kind)} claimed COMPLETE but failed structural integrity (${integrity.reason}). It was NOT cached or saved; Auto3 will continue the same wave.`,
          'warning'
        );
        return null;
      }
    }

    const conversationKey = autoBoundConversationKey || currentConversationKey();
    const handoffProject = projectNameFromAuditText(text);
    const existingProject = sanitizeProjectIdentity(autoRuntime?.projectName || '');
    const projectName = handoffProject
      ? updateRuntimeProjectName(handoffProject, 'handoff')
      : existingProject;
    const existing = readAuditResult(kind, conversationKey);
    const runId = autoRuntime?.runId || ensureAuditRunId();
    if (!runId) {
      setStatus(`${auditWaveTitle(kind)} is COMPLETE, but a durable audit run id could not be established. The handoff was not cached/saved and the wave will not advance.`, 'warning');
      return null;
    }
    const sameExisting = existing?.text === String(text).trim() && existing?.runId === runId;

    const record = {
      version: 1,
      conversationKey,
      runId,
      sourceUserId: String(sourceUserId || waveUserId(kind) || ''),
      bridgeReceipt: sameExisting && existing?.bridgeReceipt
        ? existing.bridgeReceipt
        : createBridgeReceipt(runId, kind),
      kind,
      wave: auditWaveTitle(kind),
      projectName: projectName || 'PROJECT',
      runStartedAt: autoRuntime?.startedAt || Date.now(),
      completedAt: sameExisting
        ? Number(existing?.completedAt || Date.now())
        : Date.now(),
      gateState: String(gateState || 'complete'),
      text: String(text).trim(),
      savedAt: sameExisting ? Number(existing?.savedAt || 0) : 0,
      savedFileName: sameExisting ? String(existing?.savedFileName || '') : '',
      saveError: '',
      bridgeQueuedAt: sameExisting ? Number(existing?.bridgeQueuedAt || 0) : 0,
      bridgeSavedAt: sameExisting ? Number(existing?.bridgeSavedAt || 0) : 0,
      bridgeFiles: sameExisting && Array.isArray(existing?.bridgeFiles) ? existing.bridgeFiles : [],
      bridgeError: '',
      testStatus: auditTestMetadata(text).status,
      testLimitation: auditTestMetadata(text).limitation,
      verifiedInstead: auditTestMetadata(text).verifiedInstead
    };
    // Cache acceptance is the commit point for a COMPLETE handoff. If the
    // durable runtime rejected this record (newer run/reset barrier) or userscript
    // storage failed, do not continue into rename/disk side effects with evidence
    // that the current conversation no longer owns.
    if (!writeAuditResult(record)) {
      renderAutoAuditState();
      return null;
    }

    if (projectName) {
      const renameContext = {
        source: 'handoff',
        conversationKey,
        runStartedAt: autoRuntime?.startedAt || 0
      };
      maybeRenameConversation(projectName, renameContext).catch(() => { });
      scheduleConversationTitleGuard(projectName, renameContext);
    }
    if (state?.autoSaveAuditFiles) {
      if (state.bridgeEnabled) {
        if (!sameExisting || !Number(existing?.bridgeSavedAt)) {
          enqueueBridgeAuditRecord(record);
        }
      } else {
        // Browser File System Access remains an optional fallback when the
        // localhost bridge is explicitly disabled.
        autoSaveAuditRecord(record).then(() => {
          if (kind === 'performance') autoSaveCombinedAuditIfReady(conversationKey).catch(() => { });
        }).catch(() => { });
      }
    }
    renderAutoAuditState();
    return record;
  }

  function visibleAuditLineage(turns = getChatGPTTurns(), options = {}) {
    let startIndex = -1;

    if (options.respectResetBarrier !== false && autoRuntime?.resetBarrierActive) {
      const barrierId = String(autoRuntime.resetBarrierUserId || '');
      if (!barrierId) return { core: null, second: null, performance: null, blockedByReset: true };
      startIndex = turns.findIndex(turn => getTurnId(turn) === barrierId);
      if (startIndex < 0) return { core: null, second: null, performance: null, blockedByReset: true };
    }

    let core = null;
    let second = null;
    let performance = null;

    for (let index = startIndex + 1; index < turns.length; index += 1) {
      const turn = turns[index];
      if (turnRole(turn) !== 'user') continue;
      const kind = classifyAuditTurn(turn);
      if (!kind) {
        // A non-audit user turn is a hard lineage barrier: a manual intervention
        // or repurposed conversation severs any in-progress audit chain instead
        // of stitching unrelated intents together. Assistant turns stay transparent.
        core = null;
        second = null;
        performance = null;
        continue;
      }

      // A CONTINUE command is recovery inside an already established wave. It
      // cannot create/replace a root after reload, and an orphan continuation is
      // deliberately non-resumable rather than guessed into a new run.
      if (auditTurnIsContinuation(turn)) continue;

      if (kind === 'core') {
        core = turn;
        second = null;
        performance = null;
        continue;
      }
      if (kind === 'second' && core) {
        second = turn;
        performance = null;
        continue;
      }
      if (kind === 'performance' && core && second) performance = turn;
    }

    return { core, second, performance, blockedByReset: false };
  }

  function backfillVisibleCompletedAuditResults() {
    if (detectSite().key !== 'chatgpt') return 0;
    const turns = getChatGPTTurns();
    const lineage = visibleAuditLineage(turns);
    if (lineage.blockedByReset || !lineage.core) return 0;

    let captured = 0;
    const specs = [
      ['core', 'wait-core'],
      ['second', 'wait-second'],
      ['performance', 'wait-performance']
    ];

    for (const [kind, stage] of specs) {
      const userTurn = lineage[kind];
      if (!userTurn) continue;

      const sourceUserId = getTurnId(userTurn);
      const existing = readAuditResult(kind);
      if (existing?.text && sourceUserId && existing.sourceUserId === sourceUserId) continue;

      const assistant = assistantTurnAfter(userTurn, turns);
      if (!assistant) continue;
      const gate = responseGateFromAssistantTurn(stage, assistant);
      if (gate.state !== 'complete' || !gate.text) continue;

      if (captureCompletedAudit(kind, gate.text, 'complete', sourceUserId)) {
        captured += 1;
      }
    }
    return captured;
  }

  function emptyAutoRuntime(options = {}) {
    const enabledDefault = options.enabled !== undefined
      ? Boolean(options.enabled)
      : false;

    return {
      version: 5,
      conversationKey: '',
      enabled: enabledDefault,
      stage: 'idle',
      pausedFromStage: '',
      seenUserId: '',
      anchorUserId: '',
      coreUserId: '',
      secondUserId: '',
      performanceUserId: '',
      expectedKind: '',
      runId: '',
      startedAt: 0,
      waitStartedAt: 0,
      updatedAt: Date.now(),
      stableResponseKey: '',
      stableSince: 0,
      baselineAssistantKey: '',
      resetBarrierActive: false,
      resetBarrierUserId: '',
      pausedReason: '',
      continuationKind: '',
      continuationReason: '',
      continuationPreviousUserId: '',
      partialContinuations: { core: 0, second: 0, performance: 0 },
      stallNudges: { core: 0, second: 0, performance: 0 },
      sidecarRecoveries: { core: 0, second: 0, performance: 0 },
      continueGeneratingClicks: { core: 0, second: 0, performance: 0 },
      retryClicks: { core: 0, second: 0, performance: 0 },
      idleStallKey: '',
      idleStallSince: 0,
      pendingSendReceipt: '',
      pendingSendKind: '',
      pendingSendPreviousUserId: '',
      pendingSendStartedAt: 0,
      pendingSendRetries: 0,
      pendingSendClickArmed: false,
      stageAssistantId: '',
      anchorMissingSince: 0,
      projectName: '',
      projectNameSource: '',
      renameAppliedName: '',
      renamePersistedName: '',
      renamePersistedAt: 0,
      renameAttemptName: '',
      renameAttemptCount: 0,
      completeAt: 0
    };
  }

  function normalizeAutoRuntime(parsed, conversationKey = '') {
    if (!parsed || typeof parsed !== 'object' || typeof parsed.stage !== 'string') return null;
    if (![1, 2, 3, 4, 5].includes(parsed.version)) return null;
    if (![
      'idle',
      'wait-core',
      'sending-second',
      'await-second-user',
      'wait-second',
      'sending-performance',
      'await-performance-user',
      'wait-performance',
      'sending-continuation',
      'await-continuation-user',
      'complete',
      'paused'
    ].includes(parsed.stage)) return null;

    const normalized = {
      ...emptyAutoRuntime({
        enabled: parsed.enabled !== undefined ? parsed.enabled : false
      }),
      ...parsed,
      version: 5
    };

    if (conversationKey) normalized.conversationKey = conversationKey;
    normalized.enabled = Boolean(
      parsed.enabled !== undefined ? parsed.enabled : normalized.enabled
    );

    const normalizeCounterMap = value => ({
      core: Math.max(0, Number(value?.core) || 0),
      second: Math.max(0, Number(value?.second) || 0),
      performance: Math.max(0, Number(value?.performance) || 0)
    });
    normalized.partialContinuations = normalizeCounterMap(parsed.partialContinuations);
    normalized.stallNudges = normalizeCounterMap(parsed.stallNudges);
    normalized.sidecarRecoveries = normalizeCounterMap(parsed.sidecarRecoveries);
    normalized.continueGeneratingClicks = normalizeCounterMap(parsed.continueGeneratingClicks);
    normalized.retryClicks = normalizeCounterMap(parsed.retryClicks);
    normalized.continuationKind = String(parsed.continuationKind || '');
    normalized.continuationReason = String(parsed.continuationReason || '');
    normalized.continuationPreviousUserId = String(parsed.continuationPreviousUserId || '');
    normalized.runId = String(parsed.runId || '');
    normalized.idleStallKey = String(parsed.idleStallKey || '');
    normalized.idleStallSince = Math.max(0, Number(parsed.idleStallSince) || 0);
    normalized.pendingSendReceipt = String(parsed.pendingSendReceipt || '');
    normalized.pendingSendKind = String(parsed.pendingSendKind || '');
    normalized.pendingSendPreviousUserId = String(parsed.pendingSendPreviousUserId || '');
    normalized.pendingSendStartedAt = Math.max(0, Number(parsed.pendingSendStartedAt) || 0);
    normalized.pendingSendRetries = Math.max(0, Number(parsed.pendingSendRetries) || 0);
    normalized.pendingSendClickArmed = Boolean(parsed.pendingSendClickArmed);
    normalized.resetBarrierActive = Boolean(parsed.resetBarrierActive);
    normalized.resetBarrierUserId = String(parsed.resetBarrierUserId || '');
    normalized.stageAssistantId = String(parsed.stageAssistantId || '');
    normalized.anchorMissingSince = Math.max(0, Number(parsed.anchorMissingSince) || 0);
    normalized.projectName = sanitizeProjectIdentity(parsed.projectName || '');
    normalized.projectNameSource = normalized.projectName ? String(parsed.projectNameSource || '') : '';
    normalized.renameAppliedName = normalized.projectName ? sanitizeProjectIdentity(parsed.renameAppliedName || '') : '';
    normalized.renamePersistedName = normalized.projectName ? sanitizeProjectIdentity(parsed.renamePersistedName || '') : '';
    normalized.renamePersistedAt = normalized.renamePersistedName ? Math.max(0, Number(parsed.renamePersistedAt) || 0) : 0;
    normalized.renameAttemptName = normalized.projectName ? sanitizeProjectIdentity(parsed.renameAttemptName || '') : '';
    normalized.renameAttemptCount = Math.max(0, Number(parsed.renameAttemptCount) || 0);
    return normalized;
  }

  function autoRuntimeStorageKey(conversationKey) {
    return `${AUTO_RUNTIME_PREFIX}${String(conversationKey || 'unknown')}`;
  }

  function autoLeaseStorageKey(conversationKey) {
    return `${AUTO_LEASE_PREFIX}${String(conversationKey || 'unknown')}`;
  }

  function readStartAuditHandoff() {
    try {
      const raw = sessionStorage.getItem(AUTO_START_HANDOFF_SESSION_KEY);
      if (!raw) return null;
      const parsed = JSON.parse(raw);
      if (!parsed || parsed.version !== 1) return null;
      if (parsed.tabId !== autoTabId) return null;
      if (!Number(parsed.expiresAt) || parsed.expiresAt <= Date.now()) {
        const startStillOwnsTransaction =
          parsed.tabId === autoTabId &&
          ['preparing', 'armed'].includes(parsed.phase) &&
          (auditStartInFlight || actionInFlight);

        if (!startStillOwnsTransaction) {
          sessionStorage.removeItem(AUTO_START_HANDOFF_SESSION_KEY);
          return null;
        }

        parsed.expiresAt = Date.now() + AUTO_START_PREPARE_TTL_MS;
        writeStartAuditHandoff(parsed);
      }
      return parsed;
    } catch (_) {
      return null;
    }
  }

  function writeStartAuditHandoff(handoff) {
    if (!handoff) return false;
    try {
      sessionStorage.setItem(AUTO_START_HANDOFF_SESSION_KEY, JSON.stringify(handoff));
      return true;
    } catch (_) {
      return false;
    }
  }

  function clearStartAuditHandoff() {
    startRecoveryScheduleToken = '';
    try { sessionStorage.removeItem(AUTO_START_HANDOFF_SESSION_KEY); } catch (_) { }
  }

  function beginStartAuditHandoff() {
    const sourceKey = autoBoundConversationKey || currentConversationKey();
    const snapshot = normalizeAutoRuntime(autoRuntime, sourceKey) || emptyAutoRuntime({ enabled: true });
    snapshot.enabled = true;
    snapshot.conversationKey = sourceKey;

    const now = Date.now();
    const handoff = {
      version: 1,
      tabId: autoTabId,
      sourceKey,
      lastKey: sourceKey,
      phase: 'preparing',
      startedAt: now,
      sentAt: 0,
      armedAt: 0,
      destinationKey: '',
      receipt: createAutoSendReceipt('startcore'),
      expiresAt: now + AUTO_START_PREPARE_TTL_MS,
      runtime: snapshot
    };
    if (!writeStartAuditHandoff(handoff)) return null;
    writeA3Intent(true, sourceKey, { startTransaction: true });
    return handoff;
  }

  function armStartAuditHandoffForSend(fallback = null) {
    let handoff = readStartAuditHandoff();

    if (!handoff && fallback) {
      handoff = {
        ...fallback,
        version: 1,
        tabId: autoTabId,
        phase: 'preparing'
      };
    }

    if (!handoff) return null;

    const now = Date.now();
    const key = autoBoundConversationKey || currentConversationKey();

    handoff.phase = 'armed';
    handoff.armedAt = now;
    handoff.lastKey = key;
    handoff.expiresAt = now + AUTO_START_PREPARE_TTL_MS;
    handoff.runtime = normalizeAutoRuntime(autoRuntime, key) || handoff.runtime;

    if (handoff.runtime) {
      handoff.runtime.enabled = true;
      handoff.runtime.conversationKey = key;
    }

    if (!writeStartAuditHandoff(handoff)) return null;
    return handoff;
  }

  function markStartAuditHandoffClicking(fallback = null) {
    let handoff = readStartAuditHandoff();

    if (!handoff && fallback) {
      handoff = {
        ...fallback,
        version: 1,
        tabId: autoTabId
      };
    }

    if (!handoff) return null;

    const now = Date.now();
    const key = autoBoundConversationKey || currentConversationKey();

    handoff.phase = 'clicking';
    handoff.clickAt = now;
    handoff.lastKey = key;
    handoff.expiresAt = now + AUTO_START_SENT_TTL_MS;
    handoff.runtime = normalizeAutoRuntime(autoRuntime, key) || handoff.runtime;

    if (handoff.runtime) {
      handoff.runtime.enabled = true;
      handoff.runtime.conversationKey = key;
    }

    if (!writeStartAuditHandoff(handoff)) return null;
    return handoff;
  }

  function startHandoffIsPrepared(handoff) {
    return Boolean(
      handoff &&
      ['armed', 'clicking'].includes(handoff.phase) &&
      Number(handoff.expiresAt || 0) > Date.now()
    );
  }

  function startHandoffIsCommitted(handoff) {
    return Boolean(
      handoff &&
      handoff.phase === 'sent' &&
      Number(handoff.expiresAt || 0) > Date.now()
    );
  }

  function startHandoffRouteProven(handoff, turns = null) {
    if (!handoff || Number(handoff.expiresAt || 0) <= Date.now()) return false;
    if (startHandoffIsCommitted(handoff)) return true;
    const receipt = String(handoff.receipt || '');
    if (!receipt) return false;
    const visibleTurns = turns || getChatGPTTurns();
    return Boolean(exactReceiptUserTurn(receipt, visibleTurns));
  }

  function markStartAuditHandoffSent(fallback = null) {
    let handoff = readStartAuditHandoff();

    if (!handoff && fallback) {
      handoff = {
        ...fallback,
        version: 1,
        tabId: autoTabId
      };
    }

    if (!handoff) return null;

    const now = Date.now();
    const key = autoBoundConversationKey || currentConversationKey();

    handoff.phase = 'sent';
    handoff.sentAt = now;
    handoff.expiresAt = now + AUTO_START_SENT_TTL_MS;
    handoff.lastKey = key;
    handoff.runtime = normalizeAutoRuntime(autoRuntime, key) || handoff.runtime;

    if (handoff.runtime) {
      handoff.runtime.enabled = true;
      handoff.runtime.conversationKey = key;
    }

    if (!writeStartAuditHandoff(handoff)) return null;
    return handoff;
  }

  function runtimeIsBlankDisabled(runtime) {
    if (!runtime) return true;
    return (
      !runtime.enabled &&
      runtime.stage === 'idle' &&
      !runtime.seenUserId &&
      !runtime.anchorUserId &&
      !runtime.coreUserId &&
      !runtime.secondUserId &&
      !runtime.performanceUserId &&
      !runtime.baselineAssistantKey
    );
  }

  function runtimeIsStartClaimable(runtime) {
    if (!runtime) return true;
    if (runtime.enabled) return false;

    return Boolean(
      runtime.stage === 'idle' &&
      !runtime.anchorUserId &&
      !runtime.coreUserId &&
      !runtime.secondUserId &&
      !runtime.performanceUserId &&
      !runtime.expectedKind &&
      !runtime.pendingSendReceipt &&
      !runtime.pendingSendKind &&
      !runtime.continuationKind &&
      !runtime.runId
    );
  }

  function startHandoffCanFollowRoute(handoff, previousKey, nextKey) {
    if (!handoff || !nextKey) return false;
    if (handoff.expiresAt <= Date.now()) return false;

    const routeProven = startHandoffRouteProven(handoff);
    const commitAt = Math.max(
      Number(handoff.sentAt || 0),
      Number(handoff.clickAt || 0),
      Number(handoff.armedAt || 0),
      Number(handoff.startedAt || 0)
    );
    const insideRouteCommitWindow = commitAt > 0 &&
      Date.now() - commitAt <= AUTO_START_ROUTE_COMMIT_WINDOW_MS;
    const insideHardBootstrapWindow = commitAt > 0 &&
      Date.now() - commitAt <= AUTO_START_HARD_NAV_BOOTSTRAP_MS;

    const exactStartTurnVisible = () => {
      if (!handoff.receipt) return false;
      return [...getChatGPTTurns()].reverse().some(turn =>
        classifyAuditTurn(turn) === 'core' &&
        userTurnContainsReceipt(turn, handoff.receipt)
      );
    };

    if (!previousKey) {
      if (handoff.destinationKey && handoff.destinationKey === nextKey) return true;
      if (handoff.sourceKey && handoff.sourceKey === nextKey) return true;
      if (routeProven && exactStartTurnVisible()) return true;
      if (
        routeProven &&
        insideHardBootstrapWindow &&
        !handoff.destinationKey &&
        String(handoff.sourceKey || '').startsWith('draft:') &&
        nextKey.startsWith('c:')
      ) return true;
      return false;
    }

    if (previousKey === nextKey) return false;

    const followsKnownSource =
      handoff.lastKey === previousKey ||
      handoff.sourceKey === previousKey ||
      handoff.destinationKey === previousKey;

    if ((auditStartInFlight || actionInFlight) && followsKnownSource) {
      if (exactStartTurnVisible()) return true;
      if (
        routeProven &&
        insideRouteCommitWindow &&
        !handoff.destinationKey &&
        String(handoff.sourceKey || '').startsWith('draft:') &&
        nextKey.startsWith('c:')
      ) return true;
      return false;
    }

    if (routeProven && followsKnownSource) {
      if (exactStartTurnVisible()) return true;
      if (insideRouteCommitWindow && !handoff.destinationKey && nextKey.startsWith('c:')) return true;
    }

    return false;
  }

  function migrateStartHandoffRuntime(handoff, destinationKey, destinationRuntime = null) {
    if (!handoff || !destinationKey) return destinationRuntime;

    // A brand-new destination can be touched by ChatGPT/ACB baseline bookkeeping
    // before START migration runs (seenUserId/baselineAssistantKey). That is not
    // an independent audit lineage. Only refuse takeover when the destination
    // already owns actual audit state.
    if (destinationRuntime && !runtimeIsBlankDisabled(destinationRuntime) && !runtimeIsStartClaimable(destinationRuntime)) {
      return destinationRuntime;
    }

    const sourceRuntime =
      normalizeAutoRuntime(handoff.runtime, destinationKey) ||
      emptyAutoRuntime({ enabled: true });

    sourceRuntime.enabled = true;
    sourceRuntime.conversationKey = destinationKey;
    sourceRuntime.updatedAt = Date.now();

    if (!persistRuntimeForKey(destinationKey, sourceRuntime)) return destinationRuntime;

    handoff.lastKey = destinationKey;
    if (!handoff.destinationKey && destinationKey.startsWith('c:')) {
      handoff.destinationKey = destinationKey;
    }
    handoff.runtime = normalizeAutoRuntime(sourceRuntime, destinationKey);
    handoff.expiresAt = Date.now() + (
      startHandoffIsCommitted(handoff)
        ? AUTO_START_SENT_TTL_MS
        : AUTO_START_PREPARE_TTL_MS
    );
    writeStartAuditHandoff(handoff);
    writeA3Intent(true, destinationKey, { startTransaction: true });

    return sourceRuntime;
  }

  function recoverSentStartCore(options = {}) {
    const handoff = readStartAuditHandoff();
    if (!startHandoffIsCommitted(handoff)) return false;

    bindAutoRuntimeToCurrentConversation({ claim: false });

    const currentKey = autoBoundConversationKey || currentConversationKey();
    const wasEnabled = Boolean(autoRuntime?.enabled);
    if (autoRuntime && !wasEnabled) autoRuntime.enabled = true;
    if (!claimAutoLease()) {
      if (!wasEnabled) refreshAutoRuntimeFromStorage();
      return false;
    }
    const turns = getChatGPTTurns();
    const exactStartTurn = handoff.receipt
      ? [...turns].reverse().find(turn => userTurnContainsReceipt(turn, handoff.receipt))
      : null;

    const ownsCurrentRoute = Boolean(
      currentKey === handoff.destinationKey ||
      currentKey === handoff.lastKey ||
      currentKey === handoff.sourceKey ||
      exactStartTurn
    );

    // Exact START receipt is stronger than an early/default destination runtime.
    // If route migration missed its window, bind the destination now instead of
    // leaving A3 OFF forever.
    if (exactStartTurn && currentKey.startsWith('c:') && handoff.destinationKey !== currentKey) {
      handoff.destinationKey = currentKey;
      handoff.lastKey = currentKey;

      // Preserve identity captured before START. A late route recovery often binds
      // a freshly-created destination runtime whose projectName is still blank.
      // Replacing the handoff snapshot wholesale here made the chat fall back to
      // ChatGPT's generated title (for example "Continue Core Audit").
      const carriedProjectName = sanitizeProjectIdentity(handoff.runtime?.projectName || '');
      const carriedProjectSource = carriedProjectName
        ? String(handoff.runtime?.projectNameSource || 'artifact')
        : '';
      handoff.runtime = normalizeAutoRuntime(autoRuntime, currentKey) || handoff.runtime;
      if (handoff.runtime) {
        handoff.runtime.enabled = true;
        handoff.runtime.conversationKey = currentKey;
        if (carriedProjectName && !sanitizeProjectIdentity(handoff.runtime.projectName || '')) {
          handoff.runtime.projectName = carriedProjectName;
          handoff.runtime.projectNameSource = carriedProjectSource;
        }
      }
      handoff.expiresAt = Date.now() + AUTO_START_SENT_TTL_MS;
      writeStartAuditHandoff(handoff);
      writeA3Intent(true, currentKey, { startTransaction: true });
    }

    if (ownsCurrentRoute && !autoRuntime.enabled) {
      autoRuntime.enabled = true;
      autoRuntime.conversationKey = currentKey;
      saveAutoRuntime({ pauseOnFailure: false });
      renderAutoAuditState();
    }

    const latestUser = exactStartTurn || latestChatGPTUserTurn(turns);

    if (!latestUser || classifyAuditTurn(latestUser) !== 'core') {
      if (options.finalAttempt && ownsCurrentRoute) {
        setStatus(
          'START AUDITING is still waiting for ChatGPT to hydrate the sent Core turn. A3 remains enabled; no manual toggle is required.',
          'info'
        );
      }
      return ownsCurrentRoute;
    }

    if (handoff.receipt && !userTurnContainsReceipt(latestUser, handoff.receipt)) {
      // Another Core turn may exist in this chat. Do not let START recovery
      // silently adopt the wrong audit when its exact machine receipt is absent.
      if (options.finalAttempt && ownsCurrentRoute) {
        setStatus(
          'START AUDITING destination is loaded, but its exact Core receipt is not visible yet. A3 stays enabled and recovery keeps the lineage isolated.',
          'info'
        );
      }
      return ownsCurrentRoute;
    }

    if (!autoRuntime.enabled) {
      autoRuntime.enabled = true;
      autoRuntime.conversationKey = currentKey;
      if (!saveAutoRuntime({ pauseOnFailure: false })) return false;
    }

    if (autoRuntime.stage === 'idle') {
      const armed = armFromCoreTurn(latestUser, { allowCompleted: false });
      if (!armed) return false;
    }

    if (autoRuntime.stage === 'wait-core' || autoRuntime.stage === 'paused' || autoRuntime.stage === 'complete') {
      // wait-core is the expected successful adoption. paused/complete are not
      // cleared here; they are meaningful states and must retain their own logic.
      if (autoRuntime.stage === 'wait-core') {
        claimAutoLease();
        if (!options.skipMonitor) {
          startAutoAuditMonitor({ immediate: true });
        }
        clearStartAuditHandoff();
        writeA3Intent(true, currentKey, { startTransaction: false });

        if (autoRuntime.projectName) {
          scheduleConversationTitleGuard(autoRuntime.projectName, {
            source: autoRuntime.projectNameSource || 'artifact',
            conversationKey: autoBoundConversationKey || currentConversationKey(),
            runStartedAt: autoRuntime.startedAt || 0
          });
        }

        setStatus('START AUDITING handoff recovered. Auto3 remains enabled and owns the Core -> Second -> Performance chain.', 'success');
        return true;
      }
    }

    if (options.finalAttempt) {
      setStatus('START AUDITING is still waiting for the Core turn to become adoptable. A3 state is preserved automatically; the normal observer/recovery path will continue without a manual toggle.', 'info');
    }
    return false;
  }

  function scheduleSentStartRecovery() {
    const seed = readStartAuditHandoff();
    if (!startHandoffIsCommitted(seed)) return false;

    const token = [
      seed.tabId,
      seed.sourceKey,
      seed.startedAt,
      seed.clickAt || seed.sentAt || 0
    ].join(':');

    if (startRecoveryScheduleToken === token) return true;
    startRecoveryScheduleToken = token;

    for (let index = 0; index < AUTO_START_RECOVERY_DELAYS_MS.length; index += 1) {
      const delay = AUTO_START_RECOVERY_DELAYS_MS[index];

      setTimeout(() => {
        if (startRecoveryScheduleToken !== token) return;

        const handoff = readStartAuditHandoff();
        if (!startHandoffIsCommitted(handoff)) {
          if (startRecoveryScheduleToken === token) startRecoveryScheduleToken = '';
          return;
        }

        recoverSentStartCore({
          source: 'scheduled-start-recovery',
          finalAttempt: index === AUTO_START_RECOVERY_DELAYS_MS.length - 1
        });

        if (
          index === AUTO_START_RECOVERY_DELAYS_MS.length - 1 &&
          startRecoveryScheduleToken === token
        ) {
          startRecoveryScheduleToken = '';
        }
      }, delay);
    }

    return true;
  }

  function chatGPTAuthInterstitialVisible() {
    if (detectSite().key !== 'chatgpt') return false;

    const candidates = document.querySelectorAll(
      '[role="dialog"], [aria-modal="true"], [data-testid*="modal"], ' +
      '[role="menu"], [data-state="open"], [data-radix-menu-content]'
    );

    for (const element of candidates) {
      if (!isVisible(element)) continue;
      const value = cleanTurnText(
        String(element.innerText || element.textContent || '')
      ).slice(0, 2200);

      if (
        /choose an account to continue/i.test(value) ||
        /log in to another account/i.test(value) ||
        /log in to get answers/i.test(value) ||
        /create account/i.test(value) ||
        /welcome back/i.test(value)
      ) return true;
    }

    if (!currentConversationId() && location.pathname === '/') {
      const labels = Array.from(document.querySelectorAll('button, a'))
        .filter(isVisible)
        .map(node => cleanTurnText(String(node.innerText || node.textContent || '')))
        .filter(Boolean);

      if (
        labels.some(value => /^log in$/i.test(value)) &&
        labels.some(value => /^sign up$/i.test(value))
      ) return true;
    }

    return false;
  }

  function chatGPTLoggedOutRootVisible() {
    if (detectSite().key !== 'chatgpt' || location.pathname !== '/') return false;

    const visibleControls = Array.from(document.querySelectorAll('button, a'))
      .filter(isVisible)
      .map(node => cleanTurnText(String(node.innerText || node.textContent || '')).trim())
      .filter(Boolean);

    if (visibleControls.some(value => /^log in$/i.test(value))) return true;

    const pageText = cleanTurnText(String(document.body?.innerText || '')).slice(0, 7000);
    return /log in to get answers/i.test(pageText) ||
      /choose an account to continue/i.test(pageText);
  }

  function chatGPTRootIsQuarantined() {
    return location.pathname === '/' && (
      chatGPTAuthInterstitialVisible() ||
      chatGPTLoggedOutRootVisible()
    );
  }

  function rememberStableConversationKey(key) {
    if (!/^c:[^:]+/i.test(String(key || ''))) return false;
    try {
      sessionStorage.setItem(AUTO_LAST_STABLE_CHAT_SESSION_KEY, String(key));
      return true;
    } catch (_) {
      return false;
    }
  }

  function lastStableConversationKey() {
    try {
      const value = String(sessionStorage.getItem(AUTO_LAST_STABLE_CHAT_SESSION_KEY) || '');
      return /^c:[^:]+/i.test(value) ? value : '';
    } catch (_) {
      return '';
    }
  }


  function readA3Intent() {
    try {
      const raw = sessionStorage.getItem(AUTO_A3_INTENT_SESSION_KEY);
      if (!raw) return null;
      const parsed = JSON.parse(raw);
      if (!parsed || parsed.version !== 1 || parsed.tabId !== autoTabId) return null;
      return parsed;
    } catch (_) {
      return null;
    }
  }

  function writeA3Intent(enabled, conversationKey = autoBoundConversationKey || currentConversationKey(), options = {}) {
    try {
      const payload = {
        version: 1,
        tabId: autoTabId,
        enabled: Boolean(enabled),
        conversationKey: String(conversationKey || ''),
        startTransaction: Boolean(options.startTransaction),
        updatedAt: Date.now()
      };
      sessionStorage.setItem(AUTO_A3_INTENT_SESSION_KEY, JSON.stringify(payload));
      return payload;
    } catch (_) {
      return null;
    }
  }

  function clearA3Intent() {
    try { sessionStorage.removeItem(AUTO_A3_INTENT_SESSION_KEY); } catch (_) { }
  }

  function a3IntentAllowsConversation(key, handoff = readStartAuditHandoff()) {
    const intent = readA3Intent();
    if (!intent?.enabled || !key) return false;
    if (intent.conversationKey === key) return true;

    if (!intent.startTransaction || !startHandoffIsCommitted(handoff)) return false;

    // The intent itself must belong to this exact START lineage. The old OR
    // condition treated `handoff.sourceKey === intent.conversationKey` as proof
    // for ANY destination key, so one committed START could turn A3 on in an
    // unrelated chat after a manual/sidebar route change.
    const intentOwnsHandoff = [
      String(handoff.sourceKey || ''),
      String(handoff.lastKey || ''),
      String(handoff.destinationKey || '')
    ].filter(Boolean).includes(String(intent.conversationKey || ''));

    if (!intentOwnsHandoff) return false;
    return committedStartOwnsConversationKey(handoff, key);
  }

  function adoptA3IntentForConversation(key, options = {}) {
    if (!key || !autoRuntime) return false;
    const handoff = readStartAuditHandoff();
    if (!a3IntentAllowsConversation(key, handoff)) return false;

    let changed = false;
    if (!autoRuntime.enabled) {
      autoRuntime.enabled = true;
      changed = true;
    }
    if (autoRuntime.conversationKey !== key) {
      autoRuntime.conversationKey = key;
      changed = true;
    }

    if (changed) saveAutoRuntime({ pauseOnFailure: false });

    // START owns A3 intent until the exact Core turn is adopted and the
    // handoff is cleared. Do not drop startTransaction merely because ChatGPT
    // exposed an intermediate destinationKey: route hydration can still perform
    // additional same-tab transitions before the authored Core turn appears.
    writeA3Intent(true, key, {
      startTransaction: Boolean(
        options.startTransaction ||
        (handoff && startHandoffIsCommitted(handoff))
      )
    });
    return changed;
  }

  function createDraftLifetimeId() {
    const created = globalThis.crypto && typeof globalThis.crypto.randomUUID === 'function'
      ? globalThis.crypto.randomUUID()
      : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
    autoDraftId = created;
    try { sessionStorage.setItem(AUTO_DRAFT_SESSION_KEY, created); } catch (_) { }
    return created;
  }

  function currentConversationKey() {
    const match = location.pathname.match(/^\/c\/([^/?#]+)/i);
    if (match) {
      const key = `c:${match[1]}`;
      rememberStableConversationKey(key);
      return key;
    }

    if (chatGPTRootIsQuarantined()) return `auth:${autoTabId}`;

    // Fresh root load is a draft. Never resurrect an old conversation from
    // session intent. Same-document transient route loss is handled by bind().
    return `draft:${autoTabId}:${autoDraftId}`;
  }


  function removeStoredRuntime(conversationKey) {
    if (!conversationKey) return true;
    try {
      GM_deleteValue(autoRuntimeStorageKey(conversationKey));
      return true;
    } catch (_) {
      try {
        GM_setValue(autoRuntimeStorageKey(conversationKey), '');
        return true;
      } catch (_) {
        return false;
      }
    }
  }

  function readStoredRuntime(conversationKey) {
    const key = autoRuntimeStorageKey(conversationKey);
    let raw = null;
    try {
      raw = GM_getValue(key, null);
    } catch (error) {
      return { runtime: null, found: true, corrupt: true, reason: 'storage-read-failed', raw: null };
    }
    if (raw === null || raw === undefined || raw === '') {
      return { runtime: null, found: false, corrupt: false, reason: 'absent', raw: null };
    }
    try {
      const parsed = normalizeAutoRuntime(JSON.parse(raw), conversationKey);
      if (!parsed) return { runtime: null, found: true, corrupt: true, reason: 'runtime-invalid', raw: String(raw) };
      return { runtime: parsed, found: true, corrupt: false, reason: 'valid', raw: String(raw) };
    } catch (_) {
      return { runtime: null, found: true, corrupt: true, reason: 'runtime-json-invalid', raw: String(raw) };
    }
  }

  function corruptAutoRuntime(conversationKey, reason = 'runtime-invalid') {
    const runtime = emptyAutoRuntime({ enabled: false });
    runtime.conversationKey = conversationKey;
    runtime.stage = 'paused';
    runtime.pausedReason = `CORRUPT AUTO3 RUNTIME (${reason}). Automatic send/migration is disabled until explicit Reset repairs this conversation state.`;
    runtime.storageCorrupt = true;
    return runtime;
  }

  function persistRuntimeForKey(conversationKey, runtime) {
    if (!conversationKey || !runtime) return false;

    // Never convert an invalid runtime into a silently blank/disabled one. The
    // runtime checkpoint gates irreversible Auto3 sends, so invalid input or a
    // failed storage read-back must fail closed and preserve the last durable
    // value rather than manufacturing a fresh state.
    const copy = normalizeAutoRuntime(runtime, conversationKey);
    if (!copy) return false;

    copy.version = 5;
    copy.conversationKey = conversationKey;
    copy.updatedAt = Date.now();
    const key = autoRuntimeStorageKey(conversationKey);
    try {
      const payload = JSON.stringify(copy);
      GM_setValue(key, payload);
      if (GM_getValue(key, null) !== payload) {
        throw new Error('auto-runtime read-back mismatch');
      }
      return true;
    } catch (_) {
      return false;
    }
  }

  function runtimeHasContinuity(runtime) {
    if (!runtime) return false;
    return (
      Boolean(runtime.enabled) ||
      runtime.stage !== 'idle' ||
      Boolean(runtime.seenUserId) ||
      Boolean(runtime.anchorUserId) ||
      Boolean(runtime.coreUserId) ||
      Boolean(runtime.secondUserId) ||
      Boolean(runtime.performanceUserId) ||
      Boolean(runtime.baselineAssistantKey)
    );
  }

  function loadLegacyAutoRuntimeForCurrentConversation(conversationKey) {
    try {
      const raw = GM_getValue(AUTO_LEGACY_RUNTIME_KEY, null);
      if (raw) {
        const parsed = normalizeAutoRuntime(JSON.parse(raw));
        if (parsed) {
          const turns = getChatGPTTurns();
          const anchorId = parsed.performanceUserId || parsed.secondUserId || parsed.coreUserId || parsed.anchorUserId;
          const anchorIsHere = anchorId && Boolean(findTurnById(anchorId, turns));
          const keyMatches = parsed.conversationKey === conversationKey;

          if (anchorIsHere || keyMatches || (!parsed.conversationKey && conversationKey.startsWith('draft:'))) {
            parsed.conversationKey = conversationKey;
            if (!persistRuntimeForKey(conversationKey, parsed)) return null;
            try { GM_setValue(AUTO_LEGACY_RUNTIME_KEY, ''); } catch (_) { }
            return parsed;
          }
        }
      }
    } catch (_) { }

    try {
      const legacyRaw = sessionStorage.getItem(AUTO_LEGACY_SESSION_KEY);
      if (legacyRaw) {
        const rawParsed = JSON.parse(legacyRaw);
        const rawKey = String(rawParsed?.conversationKey || '');
        const migrated = normalizeAutoRuntime(rawParsed, conversationKey);
        if (migrated) {
          const turns = getChatGPTTurns();
          const anchorId = migrated.performanceUserId || migrated.secondUserId || migrated.coreUserId || migrated.anchorUserId;
          const anchorIsHere = anchorId && Boolean(findTurnById(anchorId, turns));
          const keyMatches = Boolean(rawKey) && rawKey === conversationKey;
          const draftNoKey = !rawKey && conversationKey.startsWith('draft:');

          // The v1 session entry carries no durable conversation key, so it is
          // only adopted when its lineage is verifiable in the live DOM, or
          // when it unambiguously belongs to the current draft conversation.
          if (anchorIsHere || keyMatches || draftNoKey) {
            migrated.conversationKey = conversationKey;
            if (!persistRuntimeForKey(conversationKey, migrated)) return null;
            try { sessionStorage.removeItem(AUTO_LEGACY_SESSION_KEY); } catch (_) { }
            return migrated;
          }
        }
      }
    } catch (_) { }

    return null;
  }

  function loadAutoRuntime(conversationKey = currentConversationKey()) {
    const stored = readStoredRuntime(conversationKey);
    if (stored.runtime) {
      if (autoRuntimeCorruptKey === conversationKey) autoRuntimeCorruptKey = '';
      return stored.runtime;
    }
    if (stored.corrupt) {
      autoRuntimeCorruptKey = conversationKey;
      return corruptAutoRuntime(conversationKey, stored.reason);
    }

    const migrated = loadLegacyAutoRuntimeForCurrentConversation(conversationKey);
    if (migrated) return migrated;
    const fresh = emptyAutoRuntime();
    fresh.conversationKey = conversationKey;
    return fresh;
  }

  function saveAutoRuntime(options = {}) {
    if (!autoRuntime) return false;
    const key = autoBoundConversationKey || currentConversationKey();
    if (autoRuntimeCorruptKey === key && options.allowCorruptReplace !== true) {
      clearAutoTimers();
      releaseAutoLease(key);
      setStatus('Auto3 found corrupt durable runtime state and refused to overwrite the evidence. Use explicit Reset to repair this conversation.', 'error');
      renderAutoAuditState();
      return false;
    }

    autoRuntime.version = 5;
    autoRuntime.conversationKey = key;
    autoRuntime.updatedAt = Date.now();
    const persisted = persistRuntimeForKey(key, autoRuntime);
    if (!persisted) {
      clearAutoTimers();
      releaseAutoLease(key);
      if (options.pauseOnFailure !== false) {
        if (autoRuntime.stage !== 'paused') autoRuntime.pausedFromStage = autoRuntime.stage;
        autoRuntime.stage = 'paused';
        autoRuntime.pausedReason = 'Auto3 runtime persistence failed. No further automatic send is allowed until storage works again.';
        autoRuntime.waitStartedAt = 0;
      }
      renderAutoAuditState();
      setStatus('Auto3 persistence failed. Automation stopped before the next irreversible send; reload/Resume after userscript storage is writable again.', 'error');
      return false;
    }
    if (autoRuntimeCorruptKey === key) autoRuntimeCorruptKey = '';
    renderAutoAuditState();
    return true;
  }

  function readAutoLease(conversationKey = autoBoundConversationKey || currentConversationKey()) {
    if (!conversationKey) return null;
    try {
      const raw = GM_getValue(autoLeaseStorageKey(conversationKey), null);
      if (!raw) return null;
      const parsed = JSON.parse(raw);
      if (!parsed || typeof parsed !== 'object') return null;
      return {
        version: 1,
        ownerId: String(parsed.ownerId || ''),
        conversationKey: String(parsed.conversationKey || conversationKey),
        nonce: String(parsed.nonce || ''),
        expiresAt: Number(parsed.expiresAt) || 0,
        updatedAt: Number(parsed.updatedAt) || 0
      };
    } catch (_) {
      return null;
    }
  }

  function writeAutoLease(conversationKey, lease) {
    try {
      GM_setValue(autoLeaseStorageKey(conversationKey), JSON.stringify(lease));
      return true;
    } catch (_) {
      return false;
    }
  }

  function autoLeaseOwnedByThisTab(conversationKey = autoBoundConversationKey || currentConversationKey()) {
    const lease = readAutoLease(conversationKey);
    return Boolean(
      lease &&
      lease.ownerId === autoInstanceId &&
      lease.expiresAt > Date.now()
    );
  }

  function clearAutoLeaseTimer() {
    if (autoLeaseTimer) {
      clearTimeout(autoLeaseTimer);
      autoLeaseTimer = 0;
    }
  }

  function scheduleAutoLeaseRenewal() {
    clearAutoLeaseTimer();
    if (!autoRuntime?.enabled || !autoBoundConversationKey) return;

    autoLeaseTimer = setTimeout(() => {
      autoLeaseTimer = 0;
      if (!autoRuntime?.enabled || !autoBoundConversationKey) return;

      const lease = readAutoLease(autoBoundConversationKey);
      if (!lease || lease.ownerId !== autoInstanceId || lease.expiresAt <= Date.now()) {
        renderAutoAuditState();
        scheduleAutoAuditCheck(500);
        return;
      }

      const now = Date.now();
      writeAutoLease(autoBoundConversationKey, {
        ...lease,
        ownerId: autoInstanceId,
        conversationKey: autoBoundConversationKey,
        expiresAt: now + AUTO_LEASE_TTL_MS,
        updatedAt: now
      });
      scheduleAutoLeaseRenewal();
    }, AUTO_LEASE_RENEW_MS);
  }

  function claimAutoLease(options = {}) {
    const key = autoBoundConversationKey || currentConversationKey();
    if (!key || !autoRuntime?.enabled) return false;

    const now = Date.now();
    const current = readAutoLease(key);

    // Fast path: an unexpired lease already owned by this tab is simply kept.
    // No nonce churn, no storage write, no renewal-timer reshuffle. The
    // periodic renewal timer performs extension at its own cadence, so routine
    // evaluation cycles (every liveness check) never touch extension storage.
    if (
      !options.force &&
      current &&
      current.ownerId === autoInstanceId &&
      current.expiresAt > now + AUTO_LEASE_RENEW_MS
    ) {
      return true;
    }

    if (
      current &&
      current.expiresAt > now &&
      current.ownerId &&
      current.ownerId !== autoInstanceId &&
      !options.force
    ) {
      return false;
    }

    // Self-owned lease inside the renewal margin: extend in place and keep the
    // nonce, so pre-send fencing tokens stay stable across the extension. An
    // EXPIRED self-owned lease is dead and is re-acquired fresh below.
    if (current && current.ownerId === autoInstanceId && current.expiresAt > now) {
      const candidate = {
        ...current,
        conversationKey: key,
        expiresAt: now + AUTO_LEASE_TTL_MS,
        updatedAt: now
      };
      if (!writeAutoLease(key, candidate)) return false;
      const verified = readAutoLease(key);
      const owns = Boolean(
        verified &&
        verified.ownerId === autoInstanceId &&
        verified.nonce === current.nonce &&
        verified.expiresAt > Date.now()
      );
      if (owns) scheduleAutoLeaseRenewal();
      return owns;
    }

    const nonce = `${now.toString(36)}:${Math.random().toString(36).slice(2, 10)}`;
    const candidate = {
      version: 1,
      ownerId: autoInstanceId,
      conversationKey: key,
      nonce,
      expiresAt: now + AUTO_LEASE_TTL_MS,
      updatedAt: now
    };

    if (!writeAutoLease(key, candidate)) return false;

    const verified = readAutoLease(key);
    const owns = Boolean(
      verified &&
      verified.ownerId === autoInstanceId &&
      verified.nonce === nonce &&
      verified.expiresAt > Date.now()
    );

    if (owns) scheduleAutoLeaseRenewal();
    return owns;
  }

  // Returns a fencing token ({ conversationKey, nonce }) only when this tab
  // verifiably owns the lease. The token is carried through every async
  // automatic action and re-validated immediately before each irreversible
  // click and before each runtime commit, so a tab that lost ownership can
  // neither click Send nor persist stale state.
  async function verifyAutoLeaseForSend() {
    if (!claimAutoLease()) return null;
    await sleep(AUTO_LEASE_VERIFY_MS + Math.floor(Math.random() * 60));

    const key = autoBoundConversationKey || currentConversationKey();
    const lease = readAutoLease(key);
    if (!lease || lease.ownerId !== autoInstanceId || lease.expiresAt <= Date.now()) return null;

    const now = Date.now();
    writeAutoLease(key, {
      ...lease,
      expiresAt: now + AUTO_LEASE_TTL_MS,
      updatedAt: now
    });

    const confirmed = readAutoLease(key);
    if (
      !confirmed ||
      confirmed.ownerId !== autoInstanceId ||
      confirmed.nonce !== lease.nonce ||
      confirmed.expiresAt <= Date.now()
    ) {
      return null;
    }
    return { conversationKey: key, nonce: lease.nonce };
  }

  function isLeaseTokenCurrent(token) {
    if (!token || !token.conversationKey || !token.nonce) return false;
    const lease = readAutoLease(token.conversationKey);
    return Boolean(
      lease &&
      lease.ownerId === autoInstanceId &&
      lease.nonce === token.nonce &&
      lease.expiresAt > Date.now()
    );
  }

  function releaseAutoLease(conversationKey = autoBoundConversationKey) {
    if (!conversationKey) return;
    clearAutoLeaseTimer();

    const lease = readAutoLease(conversationKey);
    if (!lease || lease.ownerId !== autoInstanceId) return;

    writeAutoLease(conversationKey, {
      ...lease,
      ownerId: '',
      nonce: '',
      expiresAt: 0,
      updatedAt: Date.now()
    });
  }

  function refreshAutoRuntimeFromStorage() {
    if (!autoBoundConversationKey) return autoRuntime;
    const stored = readStoredRuntime(autoBoundConversationKey);
    if (stored.runtime) {
      autoRuntimeCorruptKey = '';
      autoRuntime = stored.runtime;
    } else if (stored.corrupt) {
      autoRuntimeCorruptKey = autoBoundConversationKey;
      autoRuntime = corruptAutoRuntime(autoBoundConversationKey, stored.reason);
    }
    return autoRuntime;
  }


  function committedStartOwnsConversationKey(handoff, key, options = {}) {
    if (!startHandoffIsCommitted(handoff) || !key) return false;
    if (Number(handoff.expiresAt || 0) <= Date.now()) return false;

    if ([handoff.sourceKey, handoff.lastKey, handoff.destinationKey].includes(key)) return true;

    const receipt = String(handoff.receipt || '');
    if (receipt) {
      const exact = exactReceiptUserTurn(receipt, getChatGPTTurns());
      if (exact) return true;
    }

    // ChatGPT may allocate more than one transient /c/<id> while a newly sent
    // draft is hydrating. During the bounded START bootstrap window, the explicit
    // START transaction outranks a blank/disabled destination runtime. This is
    // deliberately narrow: only draft-origin START, only stable chat routes, and
    // only while the durable handoff is fresh.
    const commitAt = Math.max(
      Number(handoff.sentAt || 0),
      Number(handoff.clickAt || 0),
      Number(handoff.armedAt || 0),
      Number(handoff.startedAt || 0)
    );
    const withinBootstrap = commitAt > 0 &&
      Date.now() - commitAt <= AUTO_START_HARD_NAV_BOOTSTRAP_MS;

    return Boolean(
      withinBootstrap &&
      !handoff.destinationKey &&
      String(handoff.sourceKey || '').startsWith('draft:') &&
      String(key).startsWith('c:') &&
      options.allowBootstrap !== false
    );
  }

  function forceCommittedStartEnabledForKey(key, runtime = autoRuntime) {
    const handoff = readStartAuditHandoff();
    if (!committedStartOwnsConversationKey(handoff, key)) return runtime;

    const claimable = runtimeIsBlankDisabled(runtime) || runtimeIsStartClaimable(runtime);
    const target = (claimable ? normalizeAutoRuntime(handoff.runtime, key) : normalizeAutoRuntime(runtime, key)) ||
      emptyAutoRuntime({ enabled: true });
    let changed = claimable;

    if (!target.enabled) {
      target.enabled = true;
      changed = true;
    }
    if (target.conversationKey !== key) {
      target.conversationKey = key;
      changed = true;
    }

    // Preserve audit stage/anchors exactly. START authority only repairs the
    // enablement/ownership bit that route hydration used to drop.
    if (changed) persistRuntimeForKey(key, target);

    writeA3Intent(true, key, { startTransaction: true });
    return target;
  }

  function activeAuditNeedsRouteProtection(runtime = autoRuntime) {
    const startHandoff = readStartAuditHandoff();
    const key = autoBoundConversationKey || currentConversationKey();
    if (
      startHandoffIsCommitted(startHandoff) &&
      committedStartOwnsConversationKey(startHandoff, key)
    ) return true;
    if (!runtime?.enabled) return false;

    // START is an active transaction before armFromCoreTurn() can move the
    // runtime out of idle. Protect it only while the current route still belongs
    // to that START lineage; an unrelated chat must never inherit route immunity.
    if (
      startHandoff &&
      (auditStartInFlight || actionInFlight) &&
      (
        [startHandoff.sourceKey, startHandoff.lastKey, startHandoff.destinationKey].includes(key) ||
        (!startHandoff.destinationKey && String(startHandoff.sourceKey || '').startsWith('draft:') && String(key).startsWith('c:'))
      )
    ) return true;

    return !['idle', 'complete'].includes(String(runtime.stage || 'idle')) ||
      Boolean(runtime.runId || runtime.coreUserId || runtime.secondUserId || runtime.performanceUserId);
  }

  function shouldPreservePreviousStableKey(previousKey, previousRuntime, authInterstitial) {
    if (!String(previousKey || '').startsWith('c:')) return false;
    if (authInterstitial) return true;
    if (!activeAuditNeedsRouteProtection(previousRuntime)) return false;

    if (!autoRouteTransientSince) autoRouteTransientSince = Date.now();
    return Date.now() - autoRouteTransientSince <= AUTO_ROUTE_TRANSIENT_GRACE_MS;
  }

  function bindAutoRuntimeToCurrentConversation(options = {}) {
    const stableMatch = location.pathname.match(/^\/c\/([^/?#]+)/i);
    const authQuarantine = chatGPTRootIsQuarantined();
    const authInterstitial = chatGPTAuthInterstitialVisible() || authQuarantine;
    const previousKey = autoBoundConversationKey;
    const previousRuntime = autoRuntime ? normalizeAutoRuntime(autoRuntime) : null;

    if (stableMatch) {
      autoRouteTransientSince = 0;
      rememberStableConversationKey(`c:${stableMatch[1]}`);
    }
    const preservePreviousStable = !stableMatch && !authQuarantine &&
      shouldPreservePreviousStableKey(previousKey, previousRuntime, authInterstitial);
    if (!stableMatch && !authQuarantine && previousKey?.startsWith('c:') && !preservePreviousStable) {
      autoRouteTransientSince = 0;
      createDraftLifetimeId();
    }

    const key = authQuarantine ? `auth:${autoTabId}` : preservePreviousStable ? previousKey : currentConversationKey();
    if (autoBoundConversationKey === key && autoRuntime) {
      if (autoRuntimeCorruptKey === key || autoRuntime.storageCorrupt) return true;
      autoRuntime = forceCommittedStartEnabledForKey(key, autoRuntime);
      return true;
    }

    if (previousKey) releaseAutoLease(previousKey);
    if (previousKey && previousKey !== key) invalidateAuditResultCache(previousKey);

    const stored = readStoredRuntime(key);
    let nextRuntime = stored.runtime;
    const storedCorrupt = Boolean(stored.corrupt);
    autoRuntimeCorruptKey = storedCorrupt ? key : '';

    const startHandoff = readStartAuditHandoff();
    if (!storedCorrupt && startHandoff && startHandoffCanFollowRoute(startHandoff, previousKey, key) &&
      (runtimeIsBlankDisabled(nextRuntime) || runtimeIsStartClaimable(nextRuntime))) {
      nextRuntime = migrateStartHandoffRuntime(startHandoff, key, nextRuntime);
    }

    if (!storedCorrupt && !nextRuntime && previousKey?.startsWith('draft:') && key.startsWith('c:') && previousRuntime && runtimeHasContinuity(previousRuntime)) {
      nextRuntime = normalizeAutoRuntime(previousRuntime, key);
      if (persistRuntimeForKey(key, nextRuntime)) {
        removeStoredRuntime(previousKey);
      } else {
        nextRuntime = emptyAutoRuntime({ enabled: false });
        nextRuntime.conversationKey = key;
        setStatus('Draft-to-conversation Auto3 migration could not be persisted. Automation is disabled in this chat until storage works and you enable it explicitly.', 'error');
      }
    }

    if (storedCorrupt) nextRuntime = corruptAutoRuntime(key, stored.reason);
    else if (!nextRuntime) nextRuntime = loadAutoRuntime(key);

    autoBoundConversationKey = key;
    if (key.startsWith('auth:')) {
      autoRuntimeCorruptKey = '';
      autoRuntime = emptyAutoRuntime({ enabled: false });
      autoRuntime.conversationKey = key;
      disconnectProjectTitleObserver();
    } else if (storedCorrupt) {
      autoRuntime = nextRuntime;
      disconnectProjectTitleObserver();
      setStatus('Auto3 durable state is corrupt for this conversation. Recovery fails closed; automatic sends and migrations are disabled until explicit Reset.', 'error');
    } else {
      autoRuntime = nextRuntime || emptyAutoRuntime();
      autoRuntime.conversationKey = key;
      autoRuntime = forceCommittedStartEnabledForKey(key, autoRuntime);
      adoptA3IntentForConversation(key);
    }

    renderAutoAuditState();
    if (!storedCorrupt && autoRuntime.projectName) {
      const renameContext = { source: autoRuntime.projectNameSource || 'artifact', conversationKey: key, runStartedAt: autoRuntime.startedAt || 0 };
      applyPersistentLocalProjectTitle(autoRuntime.projectName);
      maybeRenameConversation(autoRuntime.projectName, renameContext).catch(() => { });
    } else if (!storedCorrupt && !key.startsWith('auth:')) {
      const expectedIdentityKey = key;
      setTimeout(() => {
        if (currentConversationKey() !== expectedIdentityKey || autoBoundConversationKey !== expectedIdentityKey) return;
        try { reconcileProjectIdentityFromComposer({ rename: true }); } catch (_) { }
      }, 180);
    }

    if (options.claim !== false && autoRuntime.enabled && !storedCorrupt) claimAutoLease();
    try { console.log('[ACB-DEBUG] bind result', { key, enabled: autoRuntime?.enabled, stage: autoRuntime?.stage, projectName: autoRuntime?.projectName }); } catch (_) {}
    return true;
  }

  function clearAutoTimers() {
    if (miniAttachmentRefreshTimer) {
      clearTimeout(miniAttachmentRefreshTimer);
      miniAttachmentRefreshTimer = 0;
    }
    if (autoAuditCheckTimer) {
      clearTimeout(autoAuditCheckTimer);
      autoAuditCheckTimer = 0;
    }
    if (autoAuditNextTimer) {
      clearTimeout(autoAuditNextTimer);
      autoAuditNextTimer = 0;
    }
    if (autoComposerHoldTimer) {
      clearTimeout(autoComposerHoldTimer);
      autoComposerHoldTimer = 0;
    }
  }

  function turnRole(turn) {
    if (!turn) return '';

    const direct = String(turn.getAttribute?.('data-turn') || '').toLowerCase();
    if (direct === 'user' || direct === 'assistant') return direct;

    const own = String(turn.getAttribute?.('data-message-author-role') || '').toLowerCase();
    if (own === 'user' || own === 'assistant') return own;

    const nested = turn.querySelector?.(
      '[data-message-author-role="user"], [data-message-author-role="assistant"]'
    );
    const role = String(nested?.getAttribute('data-message-author-role') || '').toLowerCase();
    return role === 'user' || role === 'assistant' ? role : '';
  }

  function getChatGPTTurns() {
    const turns = [];
    const seenNodes = new Set();
    const seenStable = new Set();

    const add = node => {
      if (!node || seenNodes.has(node)) return;
      const role = turnRole(node);
      if (role !== 'user' && role !== 'assistant') return;
      const message = node.matches?.('[data-message-author-role]')
        ? node
        : node.querySelector?.(`[data-message-author-role="${role}"]`);
      const stableKey =
        node.getAttribute?.('data-turn-id') ||
        message?.getAttribute?.('data-message-id') ||
        node.getAttribute?.('data-testid') ||
        node.getAttribute?.('id') || '';
      if (stableKey && seenStable.has(`${role}:${stableKey}`)) return;
      seenNodes.add(node);
      if (stableKey) seenStable.add(`${role}:${stableKey}`);
      turns.push(node);
    };

    const normalizeMessage = node => node?.closest?.(
      'section[data-turn], article[data-turn], ' +
      'section[data-testid^="conversation-turn-"], article[data-testid^="conversation-turn-"], ' +
      '[data-testid^="conversation-turn-"]'
    ) || node || null;

    const root = document.querySelector('main') || document.body;
    const stableSelector =
      'section[data-turn], article[data-turn], ' +
      'section[data-testid^="conversation-turn-"], article[data-testid^="conversation-turn-"], ' +
      '[data-testid^="conversation-turn-"]';

    if (root) {
      for (const node of root.querySelectorAll(stableSelector)) add(node);
    }

    let usedFallback = false;
    if (!turns.some(turn => turnRole(turn) === 'user')) {
      usedFallback = true;
      for (const message of document.querySelectorAll(
        '[data-message-author-role="user"], [data-message-author-role="assistant"]'
      )) add(normalizeMessage(message));
    }

    if (usedFallback && turns.length > 1) {
      turns.sort((a, b) => {
        if (a === b) return 0;
        const relation = a.compareDocumentPosition(b);
        if (relation & Node.DOCUMENT_POSITION_FOLLOWING) return -1;
        if (relation & Node.DOCUMENT_POSITION_PRECEDING) return 1;
        return 0;
      });
    }
    return turns;
  }

  function getTurnId(turn) {
    if (!turn) return '';
    const message = turn.matches?.('[data-message-id]')
      ? turn
      : turn.querySelector?.('[data-message-id]');

    return String(
      turn.getAttribute?.('data-turn-id') ||
      message?.getAttribute('data-message-id') ||
      turn.getAttribute?.('data-testid') ||
      turn.getAttribute?.('id') ||
      ''
    );
  }

  function cleanTurnText(value) {
    return String(value || '')
      .replace(/\u00a0/g, ' ')
      .replace(/[\u200b\u200c\u200d\ufeff]/g, '')
      .replace(/\r\n/g, '\n')
      .trim();
  }

  function readableNodeText(node) {
    if (!node) return '';

    // innerText preserves the visual block/newline structure that the user sees.
    // textContent remains the fallback for virtualized/hidden ChatGPT surfaces.
    try {
      const visual = cleanTurnText(node.innerText);
      if (visual) return visual;
    } catch (_) { }

    return cleanTurnText(node.textContent);
  }

  // One bounded snapshot of an assistant turn per evaluation. The gate input,
  // the fallback whole-message candidates and the fingerprint all derive from
  // this single extraction pass, so a stabilization evaluation never re-reads
  // the same rendered answer a second time. The candidate bound (16) is applied
  // DURING collection: surfaces beyond it would be sliced off anyway, so their
  // text is never materialized.
  function buildAssistantSnapshot(turn) {
    if (!turn || turnRole(turn) !== 'assistant') {
      return { candidates: [], whole: [], fingerprint: '', sourceCount: 0, bestText: '' };
    }

    const message = turn.matches?.('[data-message-author-role="assistant"]')
      ? turn
      : (turn.querySelector?.('[data-message-author-role="assistant"]') || turn);
    const candidates = [];
    const seen = new Set();
    const whole = [];
    const add = value => {
      const cleaned = cleanTurnText(value);
      if (!cleaned || seen.has(cleaned)) return;
      seen.add(cleaned);
      candidates.push(cleaned);
    };
    const addWhole = value => {
      const cleaned = cleanTurnText(value);
      if (cleaned && !whole.includes(cleaned)) whole.push(cleaned);
    };

    for (const surface of message.querySelectorAll(
      '.markdown.prose, .markdown[class*="prose"], .markdown, ' +
      'pre, [data-writing-block="true"], [data-testid="writing-block-container"]'
    )) {
      const visual = readableNodeText(surface);
      const raw = String(surface.textContent || '');
      add(visual);
      if (candidates.length < 16) add(raw);
      if (candidates.length >= 16) break;
    }

    const messageVisual = readableNodeText(message);
    const messageRaw = String(message.textContent || '');
    const sameNode = turn === message;
    const turnVisual = sameNode ? messageVisual : readableNodeText(turn);
    const turnRaw = sameNode ? messageRaw : String(turn.textContent || '');

    if (candidates.length < 16) add(messageVisual);
    if (candidates.length < 16) add(messageRaw);
    if (candidates.length < 16) add(turnVisual);
    if (candidates.length < 16) add(turnRaw);

    addWhole(messageVisual);
    addWhole(messageRaw);
    addWhole(turnVisual);
    addWhole(turnRaw);

    let hash = 2166136261;
    let length = 0;
    const feed = value => {
      const text = String(value || '');
      for (let index = 0; index < text.length; index += 1) {
        hash ^= text.charCodeAt(index);
        hash = Math.imul(hash, 16777619);
      }
      length += text.length;
    };
    for (let index = 0; index < candidates.length; index += 1) {
      if (index) feed('\n\n---ACB-SURFACE---\n\n');
      feed(candidates[index]);
    }
    const fingerprint = `${length}:${(hash >>> 0).toString(36)}`;
    const bestText = whole[0] || candidates[0] || '';
    return { candidates, whole, fingerprint, sourceCount: candidates.length, bestText };
  }

  function assistantTextCandidates(turn) {
    return buildAssistantSnapshot(turn).candidates;
  }

  function userTurnTextCandidates(turn) {
    if (!turn || turnRole(turn) !== 'user') return [];

    const message = turn.matches?.('[data-message-author-role="user"]')
      ? turn
      : (turn.querySelector?.('[data-message-author-role="user"]') || turn);

    const candidates = [];
    const seen = new Set();
    const add = value => {
      const cleaned = cleanTurnText(value);
      if (!cleaned || seen.has(cleaned)) return;
      seen.add(cleaned);
      candidates.push(cleaned);
    };

    for (const node of message.querySelectorAll(
      '[data-message-content-part-type="text"], ' +
      '.user-message-bubble-color .markdown, .user-message-bubble-color, .markdown'
    )) {
      add(readableNodeText(node));
      if (candidates.length >= 12) break;
    }

    add(readableNodeText(message));
    add(message.textContent);
    if (message !== turn) {
      add(readableNodeText(turn));
      add(turn.textContent);
    }

    return candidates.slice(0, 16);
  }

  function getTurnText(turn) {
    if (!turn) return '';
    const role = turnRole(turn);
    const message = turn.matches?.(`[data-message-author-role="${role}"]`)
      ? turn
      : turn.querySelector?.(`[data-message-author-role="${role}"]`);

    if (role === 'user') {
      const candidates = userTurnTextCandidates(turn);
      if (candidates.length) return candidates[0];
    }

    if (role === 'assistant') {
      const candidates = assistantTextCandidates(turn);
      if (candidates.length) return candidates[0];
    }

    return readableNodeText(message || turn);
  }

  function latestChatGPTUserTurn(turns = getChatGPTTurns()) {
    for (let index = turns.length - 1; index >= 0; index -= 1) {
      if (turnRole(turns[index]) === 'user') return turns[index];
    }
    return null;
  }

  function latestChatGPTAssistantTurn(turns = getChatGPTTurns()) {
    for (let index = turns.length - 1; index >= 0; index -= 1) {
      if (turnRole(turns[index]) === 'assistant') return turns[index];
    }
    return null;
  }

  function activeStageAnchorId(stage = autoRuntime?.stage || '') {
    if (stage === 'wait-core') return String(autoRuntime?.coreUserId || '');
    if (stage === 'wait-second') return String(autoRuntime?.secondUserId || '');
    if (stage === 'wait-performance') return String(autoRuntime?.performanceUserId || '');
    return '';
  }

  function rememberStageAssistant(turn) {
    if (!autoRuntime || !turn || turnRole(turn) !== 'assistant') return;
    const id = getTurnId(turn);
    if (!id || autoRuntime.stageAssistantId === id) return;
    autoRuntime.stageAssistantId = id;
    autoRuntime.anchorMissingSince = 0;
    saveAutoRuntime({ pauseOnFailure: false });
  }

  function clearStageAssistant(options = {}) {
    if (!autoRuntime) return;
    autoRuntime.stageAssistantId = '';
    autoRuntime.anchorMissingSince = 0;
    if (options.save !== false) saveAutoRuntime({ pauseOnFailure: false });
  }

  function visibleUserConflictsWithActiveStage(turns = getChatGPTTurns()) {
    if (!autoRuntime) return false;
    const expected = activeWaveKind(autoRuntime.stage);
    const latestUser = latestChatGPTUserTurn(turns);
    if (!latestUser || !expected) return false;

    const visibleId = getTurnId(latestUser);
    const savedId = activeStageAnchorId(autoRuntime.stage);
    if (savedId && visibleId && savedId === visibleId) return false;

    const latestKind = classifyAuditTurn(latestUser);
    if (!latestKind) return false; // plain user prose/files are supplemental context
    return latestKind !== expected;
  }

  function recoverVirtualizedStageAssistant(turns = getChatGPTTurns()) {
    if (!autoRuntime || !['wait-core', 'wait-second', 'wait-performance'].includes(autoRuntime.stage)) {
      return null;
    }
    if (visibleUserConflictsWithActiveStage(turns)) return null;

    if (autoRuntime.stageAssistantId) {
      const remembered = findTurnById(autoRuntime.stageAssistantId, turns);
      if (remembered && turnRole(remembered) === 'assistant') return remembered;
    }

    const latest = latestChatGPTAssistantTurn(turns);
    if (latest) rememberStageAssistant(latest);
    return latest;
  }

  function assistantTurnAfter(userTurn, turns = getChatGPTTurns()) {
    if (!userTurn) return null;
    const start = turns.indexOf(userTurn);
    if (start < 0) return null;
    let result = null;
    for (let index = start + 1; index < turns.length; index += 1) {
      const kind = turnRole(turns[index]);
      if (kind === 'user') break;
      if (kind === 'assistant') result = turns[index];
    }
    return result;
  }

  function userTurnAfter(userTurn, turns = getChatGPTTurns()) {
    if (!userTurn) return null;
    const start = turns.indexOf(userTurn);
    if (start < 0) return null;
    for (let index = start + 1; index < turns.length; index += 1) {
      if (turnRole(turns[index]) === 'user') return turns[index];
    }
    return null;
  }

  function auditUserFlowAfter(anchor, expectedKind, turns = getChatGPTTurns()) {
    const result = {
      expectedTurn: null,
      conflictingTurn: null,
      conflictingKind: '',
      supplementals: [],
      lastSupplemental: null
    };
    if (!anchor) return result;

    const start = turns.indexOf(anchor);
    if (start < 0) return result;

    for (let index = start + 1; index < turns.length; index += 1) {
      const turn = turns[index];
      if (turnRole(turn) !== 'user') continue;

      const kind = classifyAuditTurn(turn);
      if (!kind) {
        result.supplementals.push(turn);
        result.lastSupplemental = turn;
        continue;
      }

      if (kind === expectedKind) {
        result.expectedTurn = turn;
        continue;
      }

      result.conflictingTurn = turn;
      result.conflictingKind = kind;
      break;
    }

    return result;
  }

  function latestExpectedAuditUserTurn(expectedKind, turns = getChatGPTTurns()) {
    if (!expectedKind) return null;
    for (let index = turns.length - 1; index >= 0; index -= 1) {
      const turn = turns[index];
      if (turnRole(turn) !== 'user') continue;
      const kind = classifyAuditTurn(turn);
      if (!kind) continue; // supplemental context never breaks lineage
      return kind === expectedKind ? turn : null; // newest real audit command wins
    }
    return null;
  }

  function latestAssistantAfterTurn(turn, turns = getChatGPTTurns()) {
    if (!turn) return null;
    const start = turns.indexOf(turn);
    if (start < 0) return null;
    let latest = null;
    for (let index = start + 1; index < turns.length; index += 1) {
      if (turnRole(turns[index]) === 'assistant') latest = turns[index];
    }
    return latest;
  }

  function assistantStronglyMatchesAuditWave(turn, kind) {
    if (!turn || turnRole(turn) !== 'assistant' || !kind) return false;
    const stage = waveWaitStage(kind);
    if (!stage) return false;

    const snapshot = buildAssistantSnapshot(turn);
    const gate = responseGateFromAssistantTurn(stage, turn, snapshot);
    if (gate.state !== 'unknown') return true;

    const text = normalizeAuditResponseText(snapshot.bestText || snapshot.candidates.join('\n\n')).slice(0, 24000);
    if (!text) return false;
    const spec = auditGateSpec(stage);
    const waveLine = text.match(/^\s*WAVE\s*:\s*(.+)$/im)?.[1] || '';
    const hasWave = Boolean(waveLine && spec?.wave?.test(waveLine));
    const hasProject = /^\s*PROJECT_NAME\s*:\s*\S/im.test(text);
    const hasTickets = /^\s*TICKETS\s*:\s*\d+\s*$/im.test(text);
    const hasHandoff = /^\s*HANDOFF\s*:\s*IMPLEMENTATION_AGENT\s*$/im.test(text);
    return hasWave && (hasTickets || hasProject || hasHandoff);
  }

  function auditAssistantAcrossSupplementals(anchor, kind, turns = getChatGPTTurns()) {
    if (!anchor || !kind) return null;

    // Strongest ownership: the normal adjacent assistant response to the audit
    // command remains authoritative even if the user adds a note afterwards.
    const adjacent = assistantTurnAfter(anchor, turns);
    if (adjacent) return adjacent;

    const start = turns.indexOf(anchor);
    if (start < 0) return null;

    let candidate = null;
    for (let index = start + 1; index < turns.length; index += 1) {
      const turn = turns[index];
      const role = turnRole(turn);

      if (role === 'user') {
        const userKind = classifyAuditTurn(turn);
        if (userKind && userKind !== kind) break;
        continue; // plain user turns are supplemental context
      }

      if (role === 'assistant' && assistantStronglyMatchesAuditWave(turn, kind)) {
        candidate = turn;
      }
    }

    return candidate;
  }

  function sidecarContextCountAfter(anchor, turns = getChatGPTTurns()) {
    if (!anchor) return 0;
    const start = turns.indexOf(anchor);
    if (start < 0) return 0;
    let count = 0;
    for (let index = start + 1; index < turns.length; index += 1) {
      const turn = turns[index];
      if (turnRole(turn) !== 'user') continue;
      if (classifyAuditTurn(turn)) continue;
      count += 1;
    }
    return count;
  }

  function findTurnById(id, turns = getChatGPTTurns()) {
    if (!id) return null;
    return turns.find(turn => getTurnId(turn) === id) || null;
  }

  const AUDIT_COMMAND_MARKERS = Object.freeze([
    {
      kind: 'performance',
      line: /^AUDIT\s+PERFORMANCE(?:\s*\/\s*STABILITY(?:\s*\/\s*EFFECTIVENESS)?)?(?:\s+CONTINUE)?(?:\s+[—–-].*)?$/i
    },
    {
      kind: 'second',
      line: /^AUDIT\s+SECOND\s+WAVE(?:\s+CONTINUE)?(?:\s+[—–-].*)?$/i
    },
    {
      kind: 'core',
      line: /^AUDIT\s+CORE(?:\s+CONTINUE)?(?:\s+[—–-].*)?$/i
    }
  ]);

  // Canonical audit-command recognizer.
  //
  // v0.0.16 accidentally used /\s+/g before split('\n'), erasing every newline.
  // That made AI ChatButtons reject its own file-delivery marker:
  //
  //   AUDIT CORE
  //   The complete command is attached as "AUDIT_CORE_x.md".
  //
  // Preserve lines and explicitly ignore fenced/quoted/list examples instead.
  function classifyAuditMessage(text) {
    const normalized = String(text || '')
      .replace(/\u00a0/g, ' ')
      .replace(/[\u200b\u200c\u200d\ufeff]/g, '')
      .replace(/\r\n?/g, '\n')
      .replace(/[ \t\f\v]+/g, ' ')
      .trim()
      .slice(0, 6000);
    if (!normalized) return '';

    // Only the FIRST meaningful authored line carries audit-command authority.
    // Scanning later lines promoted ordinary discussion/context (for example a
    // leading sentence followed by "AUDIT CORE") into an automation command, so
    // attachment-tile ordering is handled separately in classifyAuditTurn via the
    // distinct tile path and the exact ACB attachment framing below.
    let inFence = false;
    const lines = normalized.split('\n');
    for (let li = 0; li < lines.length; li += 1) {
      const line = lines[li].trim();
      if (!line) continue;

      if (/^```/.test(line)) {
        inFence = !inFence;
        continue;
      }
      if (inFence) continue;
      if (/^[>#|]/.test(line) || /^[-*]\s/.test(line)) continue;

      // First real content line is authoritative; no later line may override it.
      for (const spec of AUDIT_COMMAND_MARKERS) {
        if (spec.line.test(line)) return spec.kind;
      }
      break;
    }

    // Some ChatGPT accessibility/layout surfaces flatten attachment labels and
    // message text into one line. In that representation the canonical command
    // may no longer be first, so anchor to the exact ACB attachment framing
    // rather than to the beginning of the flattened string.
    const flat = normalized.replace(/\s+/g, ' ').trim();
    for (const [kind, pattern] of AUDIT_ATTACHMENT_PATTERNS) {
      if (pattern.test(flat)) return kind;
    }

    return '';
  }

  const AUDIT_ATTACHMENT_PATTERNS = Object.freeze([
    ['performance', /(?:^|\s)AUDIT\s+PERFORMANCE(?:\s*\/\s*STABILITY(?:\s*\/\s*EFFECTIVENESS)?)?\s+The complete command is attached as\s+"AUDIT[_\s-]*PERFORMANCE/i],
    ['second', /(?:^|\s)AUDIT\s+SECOND\s+WAVE\s+The complete command is attached as\s+"AUDIT[_\s-]*SECOND/i],
    ['core', /(?:^|\s)AUDIT\s+CORE\s+The complete command is attached as\s+"AUDIT[_\s-]*CORE/i]
  ]);

  const AUDIT_CONTINUE_MARKER_RE = /\bAUDIT\s+(?:CORE|SECOND\s+WAVE|PERFORMANCE(?:\s*\/\s*STABILITY(?:\s*\/\s*EFFECTIVENESS)?)?)\s+CONTINUE\b/i;

  function knownAuditReceiptKind(turn) {
    if (!turn || turnRole(turn) !== 'user') return '';

    // Exact receipts are stronger than surrounding composer text. START/Auto3
    // owns these tokens, so one accidental leading keystroke (for example `d`)
    // must not make a genuinely sent audit turn invisible to the state machine.
    // Never accept an arbitrary receipt-looking string: it must match the live
    // START handoff or the current committed/pending Auto3 transaction.
    const known = [];

    const start = readStartAuditHandoff();
    if (start?.receipt) known.push({ receipt: String(start.receipt), kind: 'core' });

    const committed = readCommittedAutoSend();
    if (committed?.receipt && committed?.kind) {
      known.push({ receipt: String(committed.receipt), kind: String(committed.kind) });
    }

    if (autoRuntime?.pendingSendReceipt && autoRuntime?.pendingSendKind) {
      known.push({ receipt: String(autoRuntime.pendingSendReceipt), kind: String(autoRuntime.pendingSendKind) });
    }

    for (const item of known) {
      if (!['core', 'second', 'performance'].includes(item.kind)) continue;
      if (userTurnContainsReceipt(turn, item.receipt)) return item.kind;
    }

    return '';
  }

  function classifyAuditTurn(turn) {
    if (!turn || turnRole(turn) !== 'user') return '';

    for (const candidate of userTurnTextCandidates(turn)) {
      const kind = classifyAuditMessage(candidate);
      if (kind) return kind;
    }

    const receiptKind = knownAuditReceiptKind(turn);
    if (receiptKind) return receiptKind;

    // Very large pasted commands can become an internal attachment. The
    // accessibility label is accepted only if it contains canonical framing.
    for (const tile of turn.querySelectorAll?.('[role="group"][aria-label]') || []) {
      const bigPaste = tile.querySelector?.(
        'button[name="expand-file-tile"][aria-label="Show in text field"], ' +
        'button[name="expand-file-tile"]'
      );
      if (!bigPaste) continue;

      const label = String(tile.getAttribute('aria-label') || '').trim();
      const kind = classifyAuditMessage(label);
      if (kind) return kind;
    }

    return '';
  }


  function auditTurnIsContinuation(turn) {
    if (!turn || turnRole(turn) !== 'user') return false;
    return userTurnTextCandidates(turn).some(candidate => AUDIT_CONTINUE_MARKER_RE.test(String(candidate || '')));
  }

  function textFingerprint(text) {
    const value = String(text || '');
    let hash = 2166136261;
    for (let index = 0; index < value.length; index += 1) {
      hash ^= value.charCodeAt(index);
      hash = Math.imul(hash, 16777619);
    }
    return `${value.length}:${(hash >>> 0).toString(36)}`;
  }

  function assistantFingerprint(turn, snapshot = null) {
    if (!turn) return '';
    if (turnRole(turn) !== 'assistant') return `${getTurnId(turn)}:${textFingerprint(getTurnText(turn))}`;
    const built = snapshot || buildAssistantSnapshot(turn);
    return `${getTurnId(turn)}:${built.fingerprint}`;
  }

  function chatGPTIsGenerating() {
    const root = chatGPTComposerRoot();
    if (!root) return false;
    return Boolean(root.querySelector(
      '[data-testid="stop-button"], ' +
      'button[data-testid*="stop" i], ' +
      'button[aria-label*="Stop streaming" i], ' +
      'button[aria-label*="Stop generating" i], ' +
      'button[aria-label="Stop"]'
    ));
  }

  function assistantHasFinalActions(turn) {
    if (!turn) return false;
    return Boolean(
      turn.querySelector('button[data-testid="copy-turn-action-button"][aria-label*="Copy response" i]') ||
      turn.querySelector('[aria-label="Response actions"] button[data-testid="copy-turn-action-button"]')
    );
  }

  // Authored answer content. Platform recovery chrome never lives inside these,
  // so controls found here are answer/artifact buttons, not ChatGPT recovery UI.
  const ASSISTANT_AUTHORED_CONTENT_SELECTOR =
    '.markdown, pre, code, [data-writing-block="true"], ' +
    '[data-testid="writing-block-container"], [data-message-content-part-type="text"]';

  // ChatGPT owns its response-action chrome. A plain "Retry"/"Try again" label is
  // only trusted inside that container; everywhere else it could be authored.
  const ASSISTANT_RESPONSE_ACTIONS_SELECTOR = '[aria-label="Response actions"], [data-testid="response-actions"]';

  function isAuthoredAssistantContent(element) {
    if (!element) return false;
    try {
      return Boolean(element.closest(ASSISTANT_AUTHORED_CONTENT_SELECTOR));
    } catch (_) {
      return false;
    }
  }

  // Single ChatGPT-scoped recovery lookup shared by the predicate helpers and the
  // click target. Never falls back to arbitrary descendant button text.
  function findAssistantRecoveryControl(turn, kind) {
    if (!turn) return null;
    const scope = turn.matches?.('[data-message-author-role="assistant"]')
      ? turn
      : (turn.querySelector?.('[data-message-author-role="assistant"]') || turn);

    const buttons = Array.from(scope.querySelectorAll('button'));
    for (const button of buttons) {
      if (isAuthoredAssistantContent(button)) continue;
      const label = String(button.getAttribute('aria-label') || '').trim();
      const testid = String(button.getAttribute('data-testid') || '');

      if (kind === 'continue') {
        if (/^continue generating$/i.test(label)) return button;
        if (/continue[-_\s]?generating/i.test(testid)) return button;
        continue;
      }

      if (/^retry response$/i.test(label)) return button;
      if (/retry/i.test(testid)) return button;
      // A bare "Retry" or "Try again" label is only trusted inside ChatGPT's
      // own response-action chrome. Authored content (e.g. a markdown block
      // that contains a button with such text) must never be clicked.
      if (
        /^(retry|try again)$/i.test(label) &&
        button.closest(ASSISTANT_RESPONSE_ACTIONS_SELECTOR)
      ) {
        return button;
      }
    }
    return null;
  }

  function assistantNeedsContinuation(turn) {
    return Boolean(findAssistantRecoveryControl(turn, 'continue'));
  }

  function assistantHasRetryError(turn) {
    return Boolean(findAssistantRecoveryControl(turn, 'retry'));
  }

  function assistantContinueGeneratingButton(turn) {
    return findAssistantRecoveryControl(turn, 'continue');
  }

  function assistantRetryButton(turn) {
    return findAssistantRecoveryControl(turn, 'retry');
  }

  function activeWaveKind(stage = autoRuntime?.stage || '') {
    if (stage === 'wait-core') return 'core';
    if (stage === 'wait-second') return 'second';
    if (stage === 'wait-performance') return 'performance';
    if (stage === 'sending-continuation' || stage === 'await-continuation-user') {
      return String(autoRuntime?.continuationKind || '');
    }
    return '';
  }

  function waveLabel(kind) {
    if (kind === 'core') return 'Core';
    if (kind === 'second') return 'Second Wave';
    if (kind === 'performance') return 'Performance';
    return 'Audit';
  }

  function waveWaitStage(kind) {
    if (kind === 'core') return 'wait-core';
    if (kind === 'second') return 'wait-second';
    if (kind === 'performance') return 'wait-performance';
    return '';
  }

  function waveUserId(kind) {
    if (kind === 'core') return autoRuntime?.coreUserId || '';
    if (kind === 'second') return autoRuntime?.secondUserId || '';
    if (kind === 'performance') return autoRuntime?.performanceUserId || '';
    return '';
  }

  function setWaveUserId(kind, id) {
    if (!autoRuntime || !id) return;
    if (kind === 'core') autoRuntime.coreUserId = id;
    if (kind === 'second') autoRuntime.secondUserId = id;
    if (kind === 'performance') autoRuntime.performanceUserId = id;
  }

  function bumpWaveCounter(field, kind) {
    if (!autoRuntime || !kind) return 0;
    if (!autoRuntime[field] || typeof autoRuntime[field] !== 'object') {
      autoRuntime[field] = { core: 0, second: 0, performance: 0 };
    }
    autoRuntime[field][kind] = Math.max(0, Number(autoRuntime[field][kind]) || 0) + 1;
    return autoRuntime[field][kind];
  }

  function auditContinuationPrompt(kind, attempt, reason = 'partial') {
    const marker = kind === 'core'
      ? 'AUDIT CORE CONTINUE'
      : kind === 'second'
        ? 'AUDIT SECOND WAVE CONTINUE'
        : 'AUDIT PERFORMANCE / STABILITY / EFFECTIVENESS CONTINUE';

    const statusMarker = kind === 'core'
      ? 'STATUS: AUDIT_CORE: COMPLETE'
      : kind === 'second'
        ? 'STATUS: SECOND_WAVE: COMPLETE'
        : 'STATUS: PERFORMANCE: COMPLETE';

    const ticketPrefix = kind === 'core'
      ? 'CORE'
      : kind === 'second'
        ? 'W2'
        : 'PERF';

    const maxAttempts = reason === 'stall'
      ? AUTO_MAX_STALL_NUDGES
      : reason === 'sidecar'
        ? AUTO_MAX_SIDECAR_RECOVERIES
        : AUTO_MAX_PARTIAL_CONTINUATIONS;

    const triggerLines = reason === 'stall'
      ? [
        'The previous assistant turn became idle/stopped without a trustworthy terminal audit status.',
        'Treat this as an interrupted response, not as completion and not as a request for human supervision.',
        'Continue from the exact point already reached in this SAME wave.'
      ]
      : reason === 'sidecar'
        ? [
          'One or more non-audit user messages were added while this audit wave was active.',
          'Treat those messages as supplemental context for the SAME project/target and SAME audit wave, not as a replacement task and not as a reason to request human supervision.',
          'Incorporate any relevant new facts, constraints or corrections, then continue from the exact point already reached in this SAME wave.'
        ]
        : [
          'The immediately preceding result reported PARTIAL.',
          'Treat PARTIAL as a machine-resumable checkpoint, not as a request for human supervision.',
          'Continue from the exact point already reached in this SAME wave.'
        ];

    const recoveryLabel = reason === 'stall'
      ? 'liveness recovery'
      : reason === 'sidecar'
        ? 'supplemental-context recovery'
        : 'continuation';

    return [
      `${marker} — unattended ${recoveryLabel} ${attempt}/${maxAttempts}.`,
      '',
      ...triggerLines,
      '',
      'Continue the SAME audit wave for the SAME project, target and revision in this conversation.',
      'Do not restart the wave from scratch.',
      'Reuse the project map, inspected files, evidence and conclusions already established in this conversation.',
      'Spend this continuation only on still-uncovered, interrupted or insufficiently verified high-value surface.',
      'Do not repeat already-covered analysis unless it is necessary to validate or merge a root cause.',
      'Preserve all still-valid findings already produced in earlier responses of this same wave.',
      '',
      'FINAL CONSOLIDATED HANDOFF:',
      `When this wave is exhausted, return ONE standalone final ${waveLabel(kind)} handoff in the original format.`,
      `Include ALL still-valid ${ticketPrefix} findings from every earlier response of this same wave plus newly verified findings.`,
      'Deduplicate by root cause.',
      `Renumber the final ${ticketPrefix} tickets sequentially so the final response can be handed to the implementation agent by itself.`,
      `Use ${statusMarker} only when the wave is actually complete.`,
      '',
      'If a hard execution/context limit genuinely prevents completion again, return PARTIAL again with all verified findings accumulated so far.',
      'Do not request human confirmation. The automation will continue this same wave again.',
      '',
      'Keep implementation read-only and preserve the original audit evidence, priority, scope and handoff rules.'
    ].join('\n');
  }

  function resetIdleStallWatch(options = {}) {
    if (!autoRuntime) return;
    const changed = Boolean(autoRuntime.idleStallKey || autoRuntime.idleStallSince);
    autoRuntime.idleStallKey = '';
    autoRuntime.idleStallSince = 0;
    if (changed && options.save !== false) saveAutoRuntime();
  }

  function queueSameWaveContinuation(kind, reason = 'partial') {
    if (!autoRuntime || !kind) return false;

    // A COMPLETE/BLOCKED response may finish during the idle grace window.
    // Re-check the live authored result before converting a liveness suspicion
    // into durable continuation state. This is especially important when a
    // hidden/frozen tab wakes after the assistant already finished.
    if (preemptSameWaveContinuationFromLiveResult(kind, {
      previousUserId: waveUserId(kind)
    })) return true;

    const counterField = reason === 'stall'
      ? 'stallNudges'
      : reason === 'sidecar'
        ? 'sidecarRecoveries'
        : 'partialContinuations';
    const limit = reason === 'stall'
      ? AUTO_MAX_STALL_NUDGES
      : reason === 'sidecar'
        ? AUTO_MAX_SIDECAR_RECOVERIES
        : AUTO_MAX_PARTIAL_CONTINUATIONS;
    const count = bumpWaveCounter(counterField, kind);

    if (count > limit) {
      const capLabel = reason === 'stall'
        ? 'idle-recovery'
        : reason === 'sidecar'
          ? 'supplemental-context recovery'
          : 'PARTIAL-continuation';
      pauseAutoAudit(
        `${waveLabel(kind)} exceeded the unattended ${capLabel} safety cap (${limit}). Chain stopped to prevent an infinite loop.`
      );
      return false;
    }

    clearPendingSendReceipt({ save: false });
    autoRuntime.continuationKind = kind;
    autoRuntime.continuationReason = reason;
    autoRuntime.continuationPreviousUserId = waveUserId(kind);
    autoRuntime.expectedKind = kind;
    autoRuntime.stage = 'sending-continuation';
    autoRuntime.waitStartedAt = Date.now();
    autoRuntime.stableResponseKey = '';
    autoRuntime.stableSince = 0;
    autoRuntime.stageAssistantId = '';
    autoRuntime.anchorMissingSince = 0;
    autoRuntime.idleStallKey = '';
    autoRuntime.idleStallSince = 0;
    if (!saveAutoRuntime()) return false;

    setStatus(
      reason === 'stall'
        ? `${waveLabel(kind)} appears idle/stopped without a terminal audit status. Sending a same-wave recovery nudge automatically (${count}/${limit}).`
        : reason === 'sidecar'
          ? `${waveLabel(kind)} received supplemental user context during the active wave. Continuing the SAME wave automatically (${count}/${limit}); the note remains in conversation context.`
          : `${waveLabel(kind)} reported PARTIAL. Continuing the same wave automatically (${count}/${limit}); no user action is required.`,
      'success'
    );

    scheduleAuditContinuation(kind);
    return true;
  }

  async function watchSupplementalAuditContext(kind, anchor, flow, turns = getChatGPTTurns()) {
    if (!autoRuntime || !kind || !anchor || !flow?.supplementals?.length) return false;

    if (chatGPTIsGenerating()) {
      resetIdleStallWatch();
      scheduleAutoAuditCheck(AUTO_LIVENESS_CHECK_MS);
      return true;
    }

    const lastSupplemental = flow.lastSupplemental || flow.supplementals[flow.supplementals.length - 1];
    const latestAssistant = latestAssistantAfterTurn(lastSupplemental, turns);
    const fingerprint = `sidecar:${kind}:${getTurnId(lastSupplemental) || textFingerprint(getTurnText(lastSupplemental))}:${assistantFingerprint(latestAssistant) || 'no-assistant'}`;
    const now = Date.now();

    if (autoRuntime.idleStallKey !== fingerprint) {
      autoRuntime.idleStallKey = fingerprint;
      autoRuntime.idleStallSince = now;
      saveAutoRuntime({ pauseOnFailure: false });
      setStatus(
        `${waveLabel(kind)}: supplemental user context detected inside the active audit lineage. Auto3 is waiting briefly for an audit-shaped response before deciding whether a same-wave continuation is needed.`,
        'info'
      );
      scheduleAutoAuditCheck(Math.min(AUTO_LIVENESS_CHECK_MS, AUTO_SIDECAR_RECOVERY_GRACE_MS + 100));
      return true;
    }

    const elapsed = now - (Number(autoRuntime.idleStallSince) || now);
    if (elapsed < AUTO_SIDECAR_RECOVERY_GRACE_MS) {
      scheduleAutoAuditCheck(Math.min(
        AUTO_LIVENESS_CHECK_MS,
        AUTO_SIDECAR_RECOVERY_GRACE_MS - elapsed + 100
      ));
      return true;
    }

    const ready = chatGPTComposerReadyForAutoSend();
    if (!ready.ok) {
      // A human may be adding another note/file. Do not PAUSE and never overwrite
      // the composer; simply remain in the active lineage until it becomes safe.
      setStatus(
        `${waveLabel(kind)}: supplemental context is still being edited or ChatGPT is busy. Auto3 remains armed and will continue automatically when the composer is safe.`,
        'info'
      );
      scheduleAutoAuditCheck(AUTO_LIVENESS_CHECK_MS);
      return true;
    }

    resetIdleStallWatch({ save: false });
    return queueSameWaveContinuation(kind, 'sidecar');
  }

  function auditStallFingerprint(kind, assistant) {
    return assistant
      ? `${kind}:${assistantFingerprint(assistant)}`
      : `${kind}:no-assistant:${waveUserId(kind) || 'unknown'}`;
  }

  async function watchIdleAuditStall(kind, assistant, reason = 'incomplete') {
    if (!autoRuntime || !kind) return false;

    if (chatGPTIsGenerating()) {
      resetIdleStallWatch();
      scheduleAutoAuditCheck(AUTO_LIVENESS_CHECK_MS);
      return true;
    }

    const fingerprint = auditStallFingerprint(kind, assistant);
    const now = Date.now();

    // If the response changes, ChatGPT is still making progress even if the Stop
    // control temporarily disappears. Reset the idle grace window.
    if (autoRuntime.idleStallKey !== fingerprint) {
      autoRuntime.idleStallKey = fingerprint;
      autoRuntime.idleStallSince = now;
      saveAutoRuntime();
      scheduleAutoAuditCheck(AUTO_LIVENESS_CHECK_MS);
      return true;
    }

    const elapsed = now - (Number(autoRuntime.idleStallSince) || now);
    if (elapsed < AUTO_IDLE_STALL_GRACE_MS) {
      scheduleAutoAuditCheck(Math.min(
        AUTO_LIVENESS_CHECK_MS,
        AUTO_IDLE_STALL_GRACE_MS - elapsed + 100
      ));
      return true;
    }

    const ready = chatGPTComposerReadyForAutoSend();
    if (!ready.ok) {
      if (
        ready.reason === 'ChatGPT is still generating.' ||
        ready.reason === 'Main ChatGPT composer is not available.'
      ) {
        scheduleAutoAuditCheck(AUTO_LIVENESS_CHECK_MS);
        return true;
      }

      // Never overwrite a human draft or mix our recovery with a manual attachment.
      pauseAutoAudit(
        `${waveLabel(kind)} appears stalled, but unattended recovery cannot safely use the composer: ${ready.reason}`
      );
      return true;
    }

    queueSameWaveContinuation(kind, 'stall');
    return true;
  }


  async function autoClickAssistantRecovery(turn, type, kind) {
    if (!turn || !kind) return false;
    const token = await verifyAutoLeaseForSend();
    if (!token) return false;

    const isContinue = type === 'continue';
    const button = isContinue
      ? assistantContinueGeneratingButton(turn)
      : assistantRetryButton(turn);
    if (!button || !isVisible(button) || button.disabled || button.getAttribute('aria-disabled') === 'true') {
      return false;
    }

    const field = isContinue ? 'continueGeneratingClicks' : 'retryClicks';
    const limit = isContinue ? AUTO_MAX_CONTINUE_GENERATING : AUTO_MAX_RETRIES;
    const count = bumpWaveCounter(field, kind);

    if (count > limit) {
      pauseAutoAudit(`${waveLabel(kind)} exceeded the unattended ${isContinue ? 'Continue generating' : 'Retry'} safety cap (${limit}). Chain stopped to prevent an infinite recovery loop.`);
      return false;
    }

    // Persist the recovery budget before the irreversible UI click.
    if (!saveAutoRuntime()) return false;
    if (!isLeaseTokenCurrent(token)) {
      refreshAutoRuntimeFromStorage();
      renderAutoAuditState();
      scheduleAutoAuditCheck(900);
      return false;
    }
    button.click();
    autoRuntime.waitStartedAt = Date.now();
    autoRuntime.stableResponseKey = '';
    autoRuntime.stableSince = 0;
    if (!isLeaseTokenCurrent(token)) {
      refreshAutoRuntimeFromStorage();
      renderAutoAuditState();
      scheduleAutoAuditCheck(900);
      return false;
    }
    if (!saveAutoRuntime({ pauseOnFailure: true })) return false;

    setStatus(
      `${waveLabel(kind)}: ${isContinue ? 'Continue generating' : 'Retry'} clicked automatically (${count}/${limit}).`,
      'success'
    );
    scheduleAutoAuditCheck(isContinue ? 1200 : 1800);
    return true;
  }

  function composerPlainText(input) {
    if (!input) return '';
    if ('value' in input) return String(input.value || '');
    return String(input.textContent || '').replace(/\u200b/g, '');
  }

  function chatGPTComposerStateSnapshot() {
    const root = chatGPTComposerRoot();
    if (!root) return null;
    const input = getChatGPTInput();
    return {
      rootId: String(root.id || root.getAttribute?.('data-testid') || ''),
      text: composerPlainText(input),
      tiles: chatGPTComposerAttachmentTiles(root).map(tile =>
        String(tile.getAttribute('aria-label') || '').trim().toLowerCase()
      ),
      generating: chatGPTIsGenerating()
    };
  }

  function sameComposerState(left, right) {
    if (!left || !right) return false;
    if (left.rootId !== right.rootId || Boolean(left.generating) !== Boolean(right.generating)) return false;
    if (cleanTurnText(left.text) !== cleanTurnText(right.text)) return false;
    return (
      left.tiles.length === right.tiles.length &&
      left.tiles.every(tile => right.tiles.includes(tile))
    );
  }

  // Auto3-only composer ownership guard. The empty-composer snapshot is taken
  // right before execution; every async boundary (browser yield, attachment
  // registration, send-ready wait) re-verifies that the composer still holds
  // exactly the Auto3-owned content and that the lease token is still current.
  function createAutoSendOwnershipGuard(token, initialSnapshot, options = {}) {
    const initial = initialSnapshot || chatGPTComposerStateSnapshot();
    const allowInitialAttachments = options.allowInitialAttachments === true;
    let afterWrite = null;

    return {
      initial,
      async verify() {
        if (!isLeaseTokenCurrent(token)) return false;
        const current = chatGPTComposerStateSnapshot();
        if (!current) return false;
        if (!afterWrite) {
          if (!sameComposerState(initial, current) || cleanTurnText(current.text)) return false;
          return allowInitialAttachments || !current.tiles.length;
        }
        return sameComposerState(afterWrite, current);
      },
      captureWrite() {
        afterWrite = chatGPTComposerStateSnapshot();
      }
    };
  }

  function chatGPTComposerReadyForAutoSend() {
    const site = detectSite();
    if (site.key !== 'chatgpt') return { ok: false, reason: 'ChatGPT only.' };
    if (chatGPTAuthInterstitialVisible() || chatGPTRootIsQuarantined()) {
      return { ok: false, reason: 'ChatGPT logged-out/account surface is active.' };
    }
    const input = cachedSiteElement(site, 'input');
    const root = chatGPTComposerRoot();
    if (!input || !root || root.hasAttribute('inert')) return { ok: false, reason: 'Main ChatGPT composer is not available.' };
    if (composerPlainText(input).trim()) {
      return { ok: false, reason: 'Composer contains a manual draft; automatic sending will not overwrite or append to it.' };
    }
    if (chatGPTComposerAttachmentTiles(root).length) {
      // During an active A3 audit chain the originally attached project archive
      // (and the script's own wave files) legitimately remain in the composer;
      // they ARE the audit target. Only stall when idle/standby, i.e. a genuinely
      // foreign manual attachment the user dropped in outside an audit run.
      const chainActive = Boolean(
        autoRuntime && autoRuntime.enabled &&
        autoRuntime.stage && autoRuntime.stage !== 'idle' && autoRuntime.stage !== 'complete'
      );
      try { console.log('[ACB-DEBUG] autoSend attachment gate', { chainActive, stage: autoRuntime?.stage, tiles: chatGPTComposerAttachmentTiles(root).length }); } catch (_) {}
      if (!chainActive) {
        return { ok: false, reason: 'Composer contains a pending manual attachment; automatic sending stopped to avoid mixing it with the next audit wave.' };
      }
    }
    if (chatGPTIsGenerating()) return { ok: false, reason: 'ChatGPT is still generating.' };
    return { ok: true, input, site };
  }

  function normalizeAuditResponseText(text) {
    return cleanTurnText(text)
      .normalize('NFKC')
      .replace(/[ \t]+/g, ' ')
      .replace(/\n{3,}/g, '\n\n');
  }

  function auditGateSpec(stage) {
    if (stage === 'wait-core') {
      return {
        wave: /AUDIT\s+CORE/i,
        explicit: /\bAUDIT[_\s-]*CORE\s*:\s*(COMPLETE|PARTIAL|BLOCKED)\b/i,
        status: /^\s*STATUS\s*:\s*(?:AUDIT[_\s-]*CORE|CORE)\s*:\s*(COMPLETE|PARTIAL|BLOCKED)\s*$/im,
        done: /^\s*CORE_DONE_WHEN\s*:\s*(.+)$/im
      };
    }

    if (stage === 'wait-second') {
      return {
        wave: /AUDIT\s+SECOND\s+WAVE/i,
        explicit: /\bSECOND[_\s-]*WAVE\s*:\s*(COMPLETE|PARTIAL|BLOCKED)\b/i,
        status: /^\s*STATUS\s*:\s*(?:AUDIT[_\s-]*SECOND[_\s-]*WAVE|SECOND[_\s-]*WAVE)\s*:\s*(COMPLETE|PARTIAL|BLOCKED)\s*$/im,
        done: /^\s*SECOND(?:[_\s-]*WAVE)?_DONE_WHEN\s*:\s*(.+)$/im
      };
    }

    if (stage === 'wait-performance') {
      return {
        wave: /AUDIT\s+PERFORMANCE(?:\s*\/\s*STABILITY\s*\/\s*EFFECTIVENESS)?/i,
        explicit: /\bPERFORMANCE\s*:\s*(COMPLETE|PARTIAL|BLOCKED)\b/i,
        status: /^\s*STATUS\s*:\s*(?:AUDIT[_\s-]*PERFORMANCE(?:\s*\/\s*STABILITY\s*\/\s*EFFECTIVENESS)?|PERFORMANCE)\s*:\s*(COMPLETE|PARTIAL|BLOCKED)\s*$/im,
        done: /^\s*PERFORMANCE_DONE_WHEN\s*:\s*(.+)$/im
      };
    }

    return null;
  }

  function gateState(value) {
    const normalized = String(value || '').toUpperCase();
    if (normalized === 'BLOCKED') return 'blocked';
    if (normalized === 'PARTIAL') return 'partial';
    if (normalized === 'COMPLETE') return 'complete';
    return 'unknown';
  }

  function handoffHeader(body) {
    const lines = normalizeAuditResponseText(body).split('\n');
    const header = [];
    for (const line of lines) {
      if (/^\s*\[P[012]\]\s*\[/i.test(line)) break;
      header.push(line);
      if (header.length >= 48) break;
    }
    return header.join('\n').trim();
  }

  function auditIntegritySpec(stage) {
    if (stage === 'wait-core') {
      return {
        prefix: 'CORE',
        fields: ['EVIDENCE', 'DEFECT', 'REPAIR', 'VERIFY'],
        noFindings: /^\s*NO\s+VERIFIED\s+CORE\s+DEFECTS\.?\s*$/im,
        doneLabel: 'CORE_DONE_WHEN'
      };
    }
    if (stage === 'wait-second') {
      return {
        prefix: 'W2',
        fields: ['EVIDENCE', 'DEFECT', 'REPAIR', 'VERIFY'],
        noFindings: /^\s*NO\s+NEW\s+VERIFIED\s+SECOND[-\s]*WAVE\s+DEFECTS\.?\s*$/im,
        doneLabel: 'SECOND_WAVE_DONE_WHEN'
      };
    }
    if (stage === 'wait-performance') {
      return {
        prefix: 'PERF',
        fields: ['EVIDENCE', 'ISSUE', 'OPTIMIZE', 'GUARDRAIL', 'VERIFY'],
        noFindings: /^\s*NO\s+MATERIAL\s+PERFORMANCE\s*\/\s*STABILITY\s+FINDINGS\.?\s*$/im,
        doneLabel: 'PERFORMANCE_DONE_WHEN'
      };
    }
    return null;
  }

  function auditHandoffIntegrity(stage, body, gateSpec = auditGateSpec(stage)) {
    const integritySpec = auditIntegritySpec(stage);
    if (!integritySpec || !gateSpec || !body) {
      return { valid: false, reason: 'unsupported-wave', declared: 0, found: 0 };
    }

    const scoped = normalizeAuditResponseText(body);
    const header = handoffHeader(scoped);
    const ticketsMatch = header.match(/^\s*TICKETS\s*:\s*(\d+)\s*$/im);
    if (!ticketsMatch) {
      return { valid: false, reason: 'missing-tickets-count', declared: 0, found: 0 };
    }

    const declared = Number(ticketsMatch[1]);
    if (!Number.isInteger(declared) || declared < 0 || declared > 999) {
      return { valid: false, reason: 'invalid-tickets-count', declared, found: 0 };
    }

    const doneMatch = scoped.match(gateSpec.done);
    if (!doneMatch || !String(doneMatch[1] || '').trim() || /^<.*>$/.test(String(doneMatch[1] || '').trim())) {
      return { valid: false, reason: `missing-${integritySpec.doneLabel.toLowerCase()}`, declared, found: 0 };
    }

    if (declared === 0) {
      if (!integritySpec.noFindings.test(scoped)) {
        return { valid: false, reason: 'zero-ticket-marker-missing', declared, found: 0 };
      }
      return { valid: true, reason: 'ok', declared: 0, found: 0 };
    }

    const ticketRegex = new RegExp(
      `^\\s*\\[P[012]\\]\\s*\\[${integritySpec.prefix}-(\\d{3})\\]\\s+.+$`,
      'gmi'
    );
    const matches = [...scoped.matchAll(ticketRegex)];
    if (matches.length !== declared) {
      return { valid: false, reason: `ticket-count-mismatch:${matches.length}/${declared}`, declared, found: matches.length };
    }

    const seen = new Set();
    for (let index = 0; index < matches.length; index += 1) {
      const numeric = Number(matches[index][1]);
      const expected = index + 1;
      if (numeric !== expected) {
        return { valid: false, reason: `ticket-sequence:${String(numeric).padStart(3, '0')}!=${String(expected).padStart(3, '0')}`, declared, found: matches.length };
      }
      if (seen.has(numeric)) {
        return { valid: false, reason: `duplicate-ticket:${String(numeric).padStart(3, '0')}`, declared, found: matches.length };
      }
      seen.add(numeric);

      const blockStart = matches[index].index;
      const blockEnd = index + 1 < matches.length ? matches[index + 1].index : doneMatch.index;
      if (!Number.isInteger(blockStart) || !Number.isInteger(blockEnd) || blockEnd <= blockStart) {
        return { valid: false, reason: `truncated-ticket:${integritySpec.prefix}-${String(numeric).padStart(3, '0')}`, declared, found: matches.length };
      }
      const block = scoped.slice(blockStart, blockEnd);
      for (const field of integritySpec.fields) {
        const fieldRegex = new RegExp(`^\\s*${field}\\s*:\\s*\\S`, 'im');
        if (!fieldRegex.test(block)) {
          return { valid: false, reason: `missing-${field.toLowerCase()}:${integritySpec.prefix}-${String(numeric).padStart(3, '0')}`, declared, found: matches.length };
        }
      }
    }

    const lastTicket = matches[matches.length - 1];
    if (!Number.isInteger(doneMatch.index) || doneMatch.index <= lastTicket.index) {
      return { valid: false, reason: `${integritySpec.doneLabel.toLowerCase()}-before-last-ticket`, declared, found: matches.length };
    }
    return { valid: true, reason: 'ok', declared, found: matches.length };
  }

  function concreteHandoffState(stage, body, spec) {
    if (!spec || !body) return 'unknown';
    const scoped = normalizeAuditResponseText(body);
    const header = handoffHeader(scoped);
    if (!header) return 'unknown';

    const waveLine = header.match(/^\s*WAVE\s*:\s*(.+)$/im)?.[1] || '';
    const ticketsLine = header.match(/^\s*TICKETS\s*:\s*(\d+)\s*$/im);
    const handoffLine = header.match(/^\s*HANDOFF\s*:\s*IMPLEMENTATION_AGENT\s*$/im);
    const structuredEnough = Boolean(waveLine && spec.wave.test(waveLine) && ticketsLine && handoffLine);

    if (structuredEnough) {
      const structuredStatus = header.match(spec.status);
      if (structuredStatus) {
        const terminal = gateState(structuredStatus[1]);
        if (terminal !== 'complete') return terminal;
        const integrity = auditHandoffIntegrity(stage, scoped, spec);
        return integrity.valid ? 'complete' : 'partial';
      }
    }

    const explicit = header.match(spec.explicit);
    if (explicit && (waveLine ? spec.wave.test(waveLine) : header.split('\n').length <= 16)) {
      const terminal = gateState(explicit[1]);
      if (terminal !== 'complete') return terminal;
      // A legacy explicit COMPLETE is only authoritative when it is the same
      // canonical wave status pattern. Missing STATUS remains unknown so Strict
      // mode cannot infer terminal authority from ticket shape alone.
      if (!header.match(spec.status)) return 'unknown';
      const integrity = auditHandoffIntegrity(stage, scoped, spec);
      return integrity.valid ? 'complete' : 'partial';
    }

    return 'unknown';
  }

  function responseGate(stage, text) {
    const scoped = normalizeAuditResponseText(text);
    if (!scoped) return 'unknown';
    const spec = auditGateSpec(stage);
    if (!spec) return 'unknown';

    const concrete = concreteHandoffState(stage, scoped, spec);
    if (concrete !== 'unknown') return concrete;

    const firstNonEmpty = scoped.split('\n').find(line => line.trim()) || '';
    if (/^\s*BLOCKED\s*:/i.test(firstNonEmpty) && scoped.length <= 12000) return 'blocked';
    return 'unknown';
  }

  function responseGateFromAssistantTurn(stage, turn, snapshot = null) {
    if (!turn) return { state: 'unknown', text: '', sourceCount: 0 };

    const built = snapshot || buildAssistantSnapshot(turn);
    const candidates = built.candidates;
    const wholeMessageCandidates = built.whole;

    // Whole authored answer is authoritative. It contains the real header plus all
    // tickets; nested code/pre snippets are evidence, not independent handoffs.
    for (const candidate of wholeMessageCandidates) {
      const state = responseGate(stage, candidate);
      if (state !== 'unknown') {
        return { state, text: candidate, sourceCount: candidates.length };
      }
    }

    // Fallback only for a complete handoff rendered entirely as one isolated block.
    for (const candidate of candidates) {
      const spec = auditGateSpec(stage);
      const state = concreteHandoffState(stage, candidate, spec);
      if (state !== 'unknown') {
        return { state, text: candidate, sourceCount: candidates.length };
      }
    }

    const combined = candidates.join('\n\n');
    const combinedState = concreteHandoffState(stage, combined, auditGateSpec(stage));
    return {
      state: combinedState,
      text: combined || candidates[0] || '',
      sourceCount: candidates.length
    };
  }

  function autoStageSummary() {
    if (!autoRuntime) return { text: 'Auto chain initializing for this chat...', kind: 'info' };
    if (!autoRuntime.enabled) return { text: 'Auto 3 waves is disabled for this chat. Saved progress is preserved.', kind: 'info' };

    if (chatGPTAuthInterstitialVisible()) {
      return {
        text: 'AUTH HOLD: ChatGPT account/login UI is covering this page. Auto3 remains enabled and frozen safely until the chat becomes usable again.',
        kind: 'warning'
      };
    }

    const lease = readAutoLease(autoBoundConversationKey || currentConversationKey());
    if (
      lease &&
      lease.ownerId &&
      lease.ownerId !== autoInstanceId &&
      lease.expiresAt > Date.now()
    ) {
      return {
        text: 'Standby for this conversation: another tab currently owns Auto3. This tab will take over automatically if that tab closes or its lease expires.',
        kind: 'warning'
      };
    }

    if (autoComposerHoldReason) {
      return {
        text: `Auto3 HOLD: ${autoComposerHoldReason} Waiting safely and retrying automatically; existing draft/attachments are never overwritten.`,
        kind: 'info'
      };
    }

    const continuationKind = String(autoRuntime.continuationKind || '');
    const continuationReason = String(autoRuntime.continuationReason || 'partial');
    const continuationCount = continuationKind
      ? Number(
        continuationReason === 'stall'
          ? autoRuntime.stallNudges?.[continuationKind]
          : continuationReason === 'sidecar'
            ? autoRuntime.sidecarRecoveries?.[continuationKind]
            : autoRuntime.partialContinuations?.[continuationKind]
      ) || 0
      : 0;
    const continuationLimit = continuationReason === 'stall'
      ? AUTO_MAX_STALL_NUDGES
      : continuationReason === 'sidecar'
        ? AUTO_MAX_SIDECAR_RECOVERIES
        : AUTO_MAX_PARTIAL_CONTINUATIONS;

    const labels = {
      idle: 'Armed. Waiting for a NEW latest AUDIT CORE. Active chain state is persisted across tab/browser close.',
      'wait-core': '1/3 Core is running. Waiting for COMPLETE.',
      'sending-second': 'Core COMPLETE. Preparing Audit Second Wave.',
      'await-second-user': 'Second Wave was sent. Waiting for ChatGPT to register the new user turn.',
      'wait-second': '2/3 Second Wave is running. Waiting for COMPLETE.',
      'sending-performance': 'Second Wave COMPLETE. Preparing Audit Performance.',
      'await-performance-user': 'Performance was sent. Waiting for ChatGPT to register the new user turn.',
      'wait-performance': '3/3 Performance is running. Waiting for COMPLETE.',
      'sending-continuation': continuationReason === 'stall'
        ? `${waveLabel(continuationKind)} went idle without a terminal status. Sending a recovery nudge (${continuationCount}/${continuationLimit})...`
        : continuationReason === 'sidecar'
          ? `${waveLabel(continuationKind)} received supplemental user context. Continuing the SAME wave automatically (${continuationCount}/${continuationLimit})...`
          : `${waveLabel(continuationKind)} returned PARTIAL. Continuing the SAME wave automatically (${continuationCount}/${continuationLimit})...`,
      'await-continuation-user': `${waveLabel(continuationKind)} ${continuationReason === 'stall' ? 'recovery nudge' : continuationReason === 'sidecar' ? 'supplemental-context continuation' : 'continuation'} was sent. Waiting for ChatGPT to register it.`,
      complete: '3/3 COMPLETE. All three audit waves finished; any PARTIAL/stall recovery was handled automatically.',
      paused: `Paused: ${autoRuntime.pausedReason || 'manual attention required.'}`
    };

    let text = labels[autoRuntime.stage] || `Auto chain state: ${autoRuntime.stage}`;
    if (
      autoRuntime.anchorMissingSince &&
      ['wait-core', 'wait-second', 'wait-performance'].includes(autoRuntime.stage)
    ) {
      text += ' User anchor is currently virtualized; live-response recovery is active.';
    }
    if (['wait-core', 'wait-second', 'wait-performance'].includes(autoRuntime.stage)) {
      const summaryTurns = getChatGPTTurns();
      const stageAnchor = findTurnById(activeStageAnchorId(autoRuntime.stage), summaryTurns);
      const supplementalCount = stageAnchor ? sidecarContextCountAfter(stageAnchor, summaryTurns) : 0;
      if (supplementalCount) {
        text += ` Supplemental context: ${supplementalCount} user note${supplementalCount === 1 ? '' : 's'} preserved inside this wave.`;
      }
    }
    if (
      autoRuntime.idleStallSince &&
      ['wait-core', 'wait-second', 'wait-performance'].includes(autoRuntime.stage) &&
      !chatGPTIsGenerating()
    ) {
      const remainingMs = Math.max(
        0,
        AUTO_IDLE_STALL_GRACE_MS - (Date.now() - autoRuntime.idleStallSince)
      );
      const seconds = Math.ceil(remainingMs / 1000);
      text += seconds > 0
        ? ` Idle watchdog: recovery nudge in ~${seconds}s if nothing changes.`
        : ' Idle watchdog: recovery nudge is due.';
    }
    const kind = autoRuntime.stage === 'complete'
      ? 'success'
      : autoRuntime.stage === 'paused'
        ? 'warning'
        : 'info';
    return { text, kind };
  }

  function autoProgressSnapshot() {
    const rawStage = String(autoRuntime?.stage || 'idle');
    const stage = rawStage === 'paused'
      ? String(autoRuntime?.pausedFromStage || 'paused')
      : rawStage;
    const continuationKind = String(autoRuntime?.continuationKind || '');
    const continuationStep = continuationKind === 'core'
      ? 1
      : continuationKind === 'second'
        ? 2
        : continuationKind === 'performance'
          ? 3
          : 0;
    const activeStep = stage === 'wait-core' ? 1
      : ['sending-second', 'await-second-user', 'wait-second'].includes(stage) ? 2
        : ['sending-performance', 'await-performance-user', 'wait-performance'].includes(stage) ? 3
          : ['sending-continuation', 'await-continuation-user'].includes(stage) ? continuationStep
            : rawStage === 'complete' ? 4
              : 0;
    const pausedStep = rawStage === 'paused' ? activeStep : 0;
    const recoveryStep = ['sending-continuation', 'await-continuation-user'].includes(stage)
      ? continuationStep
      : 0;

    return { rawStage, stage, activeStep, pausedStep, recoveryStep };
  }

  function renderProgressContainer(container, snapshot = autoProgressSnapshot()) {
    if (!container) return;
    const coherentResults = new Map(currentChatAuditRecords().map(record => [record.kind, record]));
    for (const step of container.querySelectorAll('[data-step]')) {
      const number = Number(step.dataset.step);
      const kind = auditKindFromStep(number);
      const result = kind ? (coherentResults.get(kind) || null) : null;
      const done = snapshot.activeStep === 4 || (snapshot.activeStep > 0 && number < snapshot.activeStep);
      const stateName = done
        ? 'done'
        : number === snapshot.pausedStep
          ? 'paused'
          : number === snapshot.recoveryStep
            ? 'recover'
            : number === snapshot.activeStep
              ? 'active'
              : 'idle';
      step.dataset.state = stateName;
      const copyReady = Boolean(result?.text && (done || snapshot.rawStage === 'complete'));
      const copied = copyReady && copiedAuditKind === kind && copiedAuditUntil > Date.now();
      step.dataset.copyReady = copyReady ? 'true' : 'false';
      step.dataset.copied = copied ? 'true' : 'false';
      step.setAttribute('role', 'button');
      step.setAttribute('tabindex', '0');

      const mini = container.id === 'acb-super-progress';
      const normalLabels = { core: 'Core', second: 'Second', performance: 'Perf' };
      step.textContent = copied
        ? (mini ? `${number}C` : `${number} COPIED`)
        : copyReady
          ? (mini ? `${number}✓` : `${number} ${normalLabels[kind]} ✓`)
          : (mini ? String(number) : `${number} ${normalLabels[kind]}`);

      step.setAttribute('aria-label', copyReady
        ? `${auditWaveTitle(kind)} complete. Click to COPY the cached handoff.`
        : `${auditWaveTitle(kind)} is ${stateName}. No completed handoff is cached yet.`);
      step.title = copyReady
        ? `COPY ${auditWaveTitle(kind)}${result.testStatus ? ` · ${result.testStatus}` : ''}${Number(result.bridgeSavedAt) > 0 ? ' · disk saved' : ''}`
        : `${auditWaveTitle(kind)} · ${stateName}`;
    }
  }


  function trivialStartComposerNoise(text) {
    const cleaned = cleanTurnText(text);
    if (!cleaned || cleaned.includes('\n')) return false;
    return Array.from(cleaned).length <= 2;
  }

  function prepareComposerForExplicitAuditStart() {
    const root = chatGPTComposerRoot();
    const input = getChatGPTInput();
    if (!root || !input || root.hasAttribute('inert')) {
      return { ok: false, reason: 'Main ChatGPT composer is not available.' };
    }

    const draft = cleanTurnText(composerPlainText(input));
    if (!draft) return { ok: true, clearedNoise: false };

    // Explicit START owns the canonical Core message, but it may absorb only a
    // microscopic accidental keystroke. Substantive user text is protected.
    if (!trivialStartComposerNoise(draft)) {
      return {
        ok: false,
        reason: 'Composer contains a manual draft. START will not merge that text into the canonical Audit Core command.'
      };
    }

    if (!smartSet(input, '')) {
      return { ok: false, reason: 'Could not clear the tiny accidental composer fragment before START.' };
    }
    if (cleanTurnText(composerPlainText(input))) {
      return { ok: false, reason: 'The accidental composer fragment could not be verified as cleared.' };
    }

    return { ok: true, clearedNoise: true, clearedText: draft };
  }

  function startHandoffComposerStillPrepared(handoff = readStartAuditHandoff()) {
    if (!handoff || !['armed', 'clicking'].includes(handoff.phase) || !handoff.receipt) return false;
    const input = getChatGPTInput();
    if (!input) return false;
    const text = cleanTurnText(composerPlainText(input));
    return Boolean(
      text &&
      classifyAuditMessage(text) === 'core' &&
      text.includes(`${AUTO_SEND_RECEIPT_PREFIX}: ${handoff.receipt}`)
    );
  }

  async function recoverArmedStartSend(options = {}) {
    if (auditStartInFlight || actionInFlight) return false;
    let handoff = readStartAuditHandoff();
    if (!handoff || !startHandoffIsPrepared(handoff)) return false;

    // A previous click may have succeeded just before navigation/unload. Exact
    // receipt evidence upgrades the checkpoint without ever clicking again.
    if (handoff.receipt && exactReceiptUserTurn(handoff.receipt, getChatGPTTurns())) {
      handoff = markStartAuditHandoffSent(handoff) || readStartAuditHandoff() || handoff;
      recoverSentStartCore({ source: 'prepared-visible-receipt', skipMonitor: true });
      scheduleSentStartRecovery();
      scheduleAutoAuditCheck(0);
      return true;
    }

    if (!startHandoffComposerStillPrepared(handoff)) return false;
    if (chatGPTIsGenerating() || chatGPTAuthInterstitialVisible() || chatGPTRootIsQuarantined()) return false;

    bindAutoRuntimeToCurrentConversation({ claim: false });
    if (!autoRuntime) return false;
    const wasEnabled = Boolean(autoRuntime.enabled);
    if (!wasEnabled) {
      // Enable only in memory so lease acquisition can be attempted without
      // writing shared runtime from a standby tab.
      autoRuntime.enabled = true;
      autoRuntime.conversationKey = autoBoundConversationKey || currentConversationKey();
    }

    const token = await verifyAutoLeaseForSend();
    if (!token) {
      if (!wasEnabled) refreshAutoRuntimeFromStorage();
      setStatus('START AUDITING is prepared, but another tab owns Auto3. This tab will not click Send; the exact receipt remains recoverable.', 'info');
      if (options.reschedule !== false) scheduleAutoAuditCheck(900);
      return false;
    }
    if (!wasEnabled) {
      writeA3Intent(true, autoRuntime.conversationKey, { startTransaction: true });
      if (!saveAutoRuntime({ pauseOnFailure: false })) return false;
    }

    const initial = chatGPTComposerStateSnapshot();
    if (!initial) return false;
    const ownership = createAutoSendOwnershipGuard(token, initial, { allowInitialAttachments: true });
    ownership.captureWrite();

    const waitMs = Math.max(250, Number(options.waitMs) || 1800);
    const send = await waitForChatGPTSendReady(waitMs);
    if (!send || !startHandoffComposerStillPrepared(handoff) || !(await ownership.verify())) {
      if (options.reschedule !== false) scheduleAutoAuditCheck(700);
      return false;
    }

    const current = getChatGPTSend();
    if (!current || !current.isConnected || current.disabled || current.getAttribute('aria-disabled') === 'true') {
      if (options.reschedule !== false) scheduleAutoAuditCheck(500);
      return false;
    }

    if (!(await ownership.verify())) return false;
    const clicking = markStartAuditHandoffClicking(handoff);
    if (!clicking) return false;

    const input = rawChatGPTComposerInput();
    const accepted = await clickChatGPTSendVerified(current, input, {
      receipt: String(clicking.receipt || ''),
      autoOwnership: ownership
    });
    if (!accepted) {
      setStatus('START AUDITING found Send, but submission is still unverified. The exact START receipt remains prepared and bounded recovery will retry without creating a second Core.', 'warning');
      if (options.reschedule !== false) scheduleAutoAuditCheck(650);
      return false;
    }

    markStartAuditHandoffSent(clicking);
    setStatus('START AUDITING verified that the prepared Core left the composer. The one-button audit chain is now running.', 'success');
    scheduleSentStartRecovery();
    startAutoAuditMonitor({ immediate: true });
    renderAutoAuditState();
    return true;
  }

  async function startAuditCoreFromReadyAttachment() {
    try { console.log('[ACB-DEBUG] START click entered', { auditStartInFlight, actionInFlight, site: detectSite().key, enabled: autoRuntime?.enabled, stage: autoRuntime?.stage, key: autoBoundConversationKey }); } catch (_) {}
    if (auditStartInFlight || actionInFlight) {
      setStatus('START AUDITING is already preparing/sending Audit Core.', 'info');
      return false;
    }
    if (detectSite().key !== 'chatgpt') {
      setStatus('START AUDITING is available only on ChatGPT.', 'warning');
      return false;
    }

    bindAutoRuntimeToCurrentConversation({ claim: false });

    const prepared = readStartAuditHandoff();
    if (startHandoffIsPrepared(prepared)) {
      setStatus('START AUDITING already has one canonical Core receipt prepared. Retrying that exact Send instead of creating another audit start.', 'info');
      return recoverArmedStartSend({ waitMs: 2500, reschedule: true });
    }

    if (autoRuntime.stage !== 'idle') {
      if (autoRuntime.stage === 'complete') {
        // A repeated START in the same chat always means a brand-new audit. The
        // previous run is obsolete, so clear its stale 3/3 DONE and start fresh
        // instead of refusing. Reset keeps Auto3 enabled and drops the old cached
        // lineage so the previous "3/3 COMPLETE" is never shown for this audit.
        if (!resetAutoAuditRuntime({ silent: true })) {
          setStatus('START AUDITING could not clear the previous completed run to begin a new audit.', 'error');
          renderAutoAuditState();
          return false;
        }
      } else {
        setStatus(`START AUDITING ignored: this chat is already in ${superCompactAutoLabel()} state.`, 'warning');
        renderAutoAuditState();
        return false;
      }
    } else {
      resetAutoAuditRuntime({ silent: true });
    }
    if (chatGPTIsGenerating()) {
      setStatus('START AUDITING is waiting because ChatGPT is already generating.', 'warning');
      renderAutoAuditState();
      return false;
    }

    const attachment = chatGPTReadyAttachmentSummary();
    if (!attachment.ready) {
      setStatus(`START AUDITING is not ready: ${attachment.reason}`, 'warning');
      renderAutoAuditState();
      return false;
    }
    const composerPrep = prepareComposerForExplicitAuditStart();
    if (!composerPrep.ok) {
      setStatus(`START AUDITING is not ready: ${composerPrep.reason}`, 'warning');
      renderAutoAuditState();
      return false;
    }

    if (!autoRuntime) autoRuntime = emptyAutoRuntime({ enabled: false });
    const wasEnabled = Boolean(autoRuntime.enabled);
    if (!wasEnabled) {
      autoRuntime.enabled = true;
      autoRuntime.conversationKey = autoBoundConversationKey || currentConversationKey();
    }

    const token = await verifyAutoLeaseForSend();
    if (!token) {
      if (!wasEnabled) refreshAutoRuntimeFromStorage();
      setStatus('START AUDITING did not acquire the verified Auto3 lease. Another tab may own this chat; nothing was written or sent.', 'warning');
      renderAutoAuditState();
      scheduleAutoAuditCheck(900);
      return false;
    }
    if (!wasEnabled) {
      writeA3Intent(true, autoRuntime.conversationKey, { startTransaction: true });
      if (!saveAutoRuntime({ pauseOnFailure: false })) {
        setStatus('START AUDITING acquired ownership but could not persist its Auto3 arming checkpoint. Nothing was sent.', 'error');
        renderAutoAuditState();
        return false;
      }
    }

    reconcileProjectIdentityFromComposer({ rename: true });
    const preset = findAuditPreset('core');
    if (!preset) {
      setStatus('START AUDITING failed: the canonical Audit Core preset is missing. Reload/reconcile defaults first.', 'error');
      return false;
    }

    const startHandoff = beginStartAuditHandoff();
    if (!startHandoff) {
      setStatus('START AUDITING could not create its durable handoff checkpoint. Nothing was sent; A3 state was left unchanged.', 'error');
      renderAutoAuditState();
      return false;
    }

    const initialSnapshot = chatGPTComposerStateSnapshot();
    if (!initialSnapshot) {
      clearStartAuditHandoff();
      setStatus('START AUDITING lost the live composer before preparation. Nothing was sent.', 'warning');
      return false;
    }
    const ownership = createAutoSendOwnershipGuard(token, initialSnapshot, { allowInitialAttachments: true });

    auditStartInFlight = true;
    renderAutoAuditState();
    try {
      const startPreset = { ...preset, machineReceipt: startHandoff.receipt };
      const result = await executePreset(startPreset, 'run', {
        canonicalComposerOnly: true,
        autoOwnership: ownership,
        beforeSend: async () => Boolean(armStartAuditHandoffForSend(startHandoff)),
        beforeClick: async () => Boolean(isLeaseTokenCurrent(token) && markStartAuditHandoffClicking(startHandoff))
      });

      if (!result?.sent) {
        const pending = readStartAuditHandoff();
        if (startHandoffComposerStillPrepared(pending)) {
          setStatus(`START AUDITING prepared Core but Send is not positively verified yet (${result?.reason || 'not ready'}). The exact receipt is preserved for lease-fenced retry.`, 'warning');
          setTimeout(() => {
            recoverArmedStartSend({ waitMs: 2500, reschedule: true }).catch(() => scheduleAutoAuditCheck(900));
          }, 80);
          return false;
        }
        if (pending && !startHandoffIsCommitted(pending)) {
          writeA3Intent(true, autoRuntime?.conversationKey || currentConversationKey(), { startTransaction: true });
        }
        setStatus(`START AUDITING prepared Core (${result?.reason || 'waiting for send'}). A3 intent is preserved.`, 'info');
        return false;
      }

      markStartAuditHandoffSent(startHandoff);
      setStatus(`START AUDITING sent Audit Core with ${attachment.count} project attachment${attachment.count === 1 ? '' : 's'}. Preserving Auto3 across ChatGPT route hydration...`, 'success');
      scheduleSentStartRecovery();
      bindAutoRuntimeToCurrentConversation({ claim: false });
      recoverSentStartCore({ source: 'immediate-post-send' });
      if (!autoRuntime.enabled) {
        autoRuntime.enabled = true;
        autoRuntime.conversationKey = autoBoundConversationKey || currentConversationKey();
        saveAutoRuntime({ pauseOnFailure: false });
      }
      writeA3Intent(true, autoRuntime.conversationKey, { startTransaction: true });
      startAutoAuditMonitor({ immediate: true });
      renderAutoAuditState();
      if (autoRuntime?.projectName) {
        scheduleConversationTitleGuard(autoRuntime.projectName, {
          source: autoRuntime.projectNameSource || 'artifact',
          conversationKey: autoBoundConversationKey || currentConversationKey(),
          runStartedAt: autoRuntime.startedAt || 0
        });
      }
      return true;
    } finally {
      auditStartInFlight = false;
      const pending = readStartAuditHandoff();
      if (pending && !startHandoffIsCommitted(pending) && !startHandoffIsPrepared(pending)) clearStartAuditHandoff();
      renderAutoAuditState();
    }
  }

  function currentChatShouldBePreservedForNewChat() {
    const snapshot = chatGPTComposerStateSnapshot();
    const hasDraft = Boolean(cleanTurnText(snapshot?.text || ''));
    const hasAttachments = Boolean(snapshot?.tiles?.length);
    const activeStage = Boolean(
      autoRuntime?.enabled &&
      !['idle', 'complete'].includes(String(autoRuntime.stage || 'idle'))
    );

    return {
      preserve: hasDraft || hasAttachments || chatGPTIsGenerating() || activeStage,
      hasDraft,
      hasAttachments,
      activeStage,
      generating: chatGPTIsGenerating()
    };
  }

  function openFreshChatInNewTab() {
    try {
      const link = document.createElement('a');
      link.href = `${location.origin}/`;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      link.style.display = 'none';
      document.body.appendChild(link);
      link.click();
      link.remove();
      return true;
    } catch (_) {
      return false;
    }
  }

  function openNewChatFromWidget() {
    if (detectSite().key !== 'chatgpt') {
      setStatus('New Chat shortcut is available only on ChatGPT.', 'warning');
      return false;
    }

    bindAutoRuntimeToCurrentConversation({ claim: false });
    if (autoRuntime) saveAutoRuntime({ pauseOnFailure: false });

    try {
      const nativeNewChat = document.querySelector('a[href="/"], button[aria-label*="New chat" i], [data-testid="create-new-chat-button"]');
      if (nativeNewChat && typeof nativeNewChat.click === 'function') {
        nativeNewChat.click();
        setStatus('Opened New Chat in current tab.', 'success');
        return true;
      }
    } catch (_) { }

    try {
      if (location.pathname !== '/') {
        location.assign(`${location.origin}/`);
      } else {
        location.reload();
      }
      setStatus('Opened New Chat in current tab.', 'success');
      return true;
    } catch (_) {
      setStatus('New Chat navigation failed.', 'warning');
      return false;
    }
  }

  function currentChatAuditRecords(conversationKey = autoBoundConversationKey || currentConversationKey()) {
    if (autoRuntime && autoRuntime.conversationKey === conversationKey && autoRuntime.resetBarrierActive) return [];
    const storedRuntime = readStoredRuntime(conversationKey);
    if (storedRuntime.corrupt) return [];
    if (storedRuntime.runtime?.resetBarrierActive && (!autoRuntime || autoRuntime.conversationKey !== conversationKey)) return [];
    const orderedKinds = ['core', 'second', 'performance'];
    const all = orderedKinds
      .map(kind => readAuditResult(kind, conversationKey))
      .filter(record => Boolean(record?.text));
    if (!all.length) return [];

    // Even one cached wave must satisfy lineage. Returning a lone Second or
    // Performance record used to let idle recovery treat orphaned evidence as a
    // valid chain and skip directly toward Performance/COMPLETE after a partial
    // storage failure or manual key deletion.
    const groups = new Map();
    for (const record of all) {
      const runId = String(record.runId || '');
      // Pre-runId/partially corrupted records have no proof they belong to the
      // same execution. Never manufacture an ALL_3 lineage by grouping every
      // missing-run record under one synthetic key.
      const key = runId || `__legacy__:${record.kind}`;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(record);
    }

    let selected = null;
    const runtimeRunId = (
      autoRuntime &&
      String(autoRuntime.conversationKey || '') === String(conversationKey) &&
      autoRuntime.runId
    ) ? String(autoRuntime.runId) : '';

    if (runtimeRunId && groups.has(runtimeRunId)) {
      selected = groups.get(runtimeRunId);
    } else if (runtimeRunId) {
      return [];
    } else {
      if (
        autoRuntime &&
        ['wait-core', 'sending-second', 'await-second-user', 'wait-second', 'sending-performance', 'await-performance-user', 'wait-performance'].includes(autoRuntime.stage)
      ) {
        return [];
      }
      // Without a bound run, recover the newest cached lineage as a whole instead
      // of manufacturing an impossible Core/Second/Performance combination.
      selected = [...groups.values()].sort((left, right) => {
        const newest = records => Math.max(...records.map(record => Number(record.completedAt || 0)), 0);
        return newest(right) - newest(left);
      })[0] || [];
    }

    const byKind = Object.fromEntries(selected.map(record => [record.kind, record]));

    if (!byKind.core) return [];

    const coherent = [byKind.core];
    if (byKind.second) coherent.push(byKind.second);
    if (byKind.performance && byKind.second) coherent.push(byKind.performance);
    return coherent;
  }

  function setAuditAutoSaveEnabled(_next, source = 'UI') {
    // Compatibility entry point only. Auto-save is a hard invariant now.
    if (!commitStateMutation(
      () => { state.autoSaveAuditFiles = true; },
      'Audit auto-save invariant could not be persisted; the previous state was restored.'
    )) return false;

    renderAutoAuditState();
    setStatus(`Auto SAVE on COMPLETE is always enabled (${source}).`, 'success');

    const conversationKey = autoBoundConversationKey || currentConversationKey();
    if (state.bridgeEnabled) {
      for (const record of currentChatAuditRecords(conversationKey)) {
        if (!Number(record.bridgeSavedAt)) enqueueBridgeAuditRecord(record);
      }
      flushBridgeQueue({ force: true, conversationKey }).catch(() => { });
    } else {
      flushCurrentAuditResultsToFolder().catch(() => { });
    }
    return true;
  }

  async function saveCurrentChatAuditsNow(options = {}) {
    const conversationKey = autoBoundConversationKey || currentConversationKey();

    if (options.refreshVisible !== false) {
      backfillVisibleCompletedAuditResults();
    }

    const records = currentChatAuditRecords(conversationKey);
    if (!records.length) {
      setStatus(
        options.manualSync
          ? 'SYNC/SAVE: runtime state refreshed and persisted; this chat has no structurally COMPLETE audit wave to write yet.'
          : 'SAVE: this chat has no cached COMPLETE audit handoff yet.',
        options.manualSync ? 'info' : 'warning'
      );
      renderAutoAuditState();
      return false;
    }

    if (!state.bridgeEnabled) {
      const result = await flushCurrentAuditResultsToFolder({ force: true });
      setStatus(
        `${options.manualSync ? 'SYNC/SAVE' : 'SAVE'}: browser fallback wrote ${result.saved}/${result.ready} COMPLETE wave(s)${result.combined ? ' + refreshed ALL_3' : ''}.`,
        result.saved === result.ready ? 'success' : 'warning'
      );
      renderAutoAuditState();
      return result.saved > 0;
    }

    // Manual forceAll is a fresh DELIVERY BATCH, not merely another receipt
    // for the historical audit run. Some bridges deduplicate by run_id + wave,
    // so every click gets a new delivery run_id shared by Core/Second/Perf.
    // That preserves ALL_3 grouping while forcing the server down the write path.
    const materializeRunId = options.forceAll
      ? createBridgeMaterializeRunId()
      : '';

    if (options.forceAll) {
      const validRuns = new Set(records.map(record => String(record.runId || '')).filter(Boolean));
      let superseded = 0;
      for (const job of listBridgeJobs()) {
        if (
          job.materialize &&
          job.conversationKey === conversationKey &&
          validRuns.has(String(job.sourceRunId || job.runId || '')) &&
          !job.deliveredAwaitingAck &&
          !Number(job.inFlightAt || 0)
        ) {
          // Retire permanent rejections only once their error is already preserved
          // in canonical audit-result state; otherwise keep the actionable error
          // visible. This bounds terminal queue growth across repeated manual SYNC/SAVE.
          if (job.permanent) {
            const canonical = readAuditResultFresh(job.wave, job.conversationKey);
            if (!canonical || !canonical.bridgeError) continue;
          }
          if (deleteBridgeJob(job.jobId, { signal: false })) superseded += 1;
        }
      }
      if (superseded) signalBridgeQueueChange();
    }

    for (const record of records) {
      if (options.forceAll) {
        enqueueBridgeAuditRecord(record, {
          force: true,
          freshReceipt: true,
          deliveryRunId: materializeRunId,
          deferFlush: true
        });
        continue;
      }

      if (Number(record.bridgeSavedAt) > 0) continue;
      const queued = record.bridgeReceipt ? readBridgeJob(record.bridgeReceipt) : null;
      if (!queued) enqueueBridgeAuditRecord(record, { deferFlush: true });
    }

    // Health/auth check only. Manual SYNC/SAVE owns the flush so there is no
    // competing 0ms background worker racing for the same lease.
    const connected = await checkBridge({
      force: true,
      suppressFlush: true
    });

    if (connected) {
      await flushBridgeQueueManualReliable(conversationKey, { maxAttempts: 5 });
    }

    const stats = bridgeQueueStats(conversationKey);
    const refreshed = currentChatAuditRecords(conversationKey);
    const durable = refreshed.filter(record => Number(record.bridgeSavedAt) > 0).length;
    const allThree = refreshed.length === 3;
    const performance = refreshed.find(record => record.kind === 'performance');
    const combinedConfirmed = Boolean(
      performance?.combinedSavedAt ||
      performance?.combinedFileName
    );

    if (stats.failed > 0) {
      setStatus(
        `${options.manualSync ? 'SYNC/SAVE' : 'SAVE'}: ${durable}/${records.length} wave(s) durable; ${stats.failed} job(s) need bridge/token/config attention. Cached audit text and runtime state are retained.`,
        'error'
      );
      renderAutoAuditState();
      return false;
    }

    if (stats.pending > 0 || !connected) {
      setStatus(
        `${options.manualSync ? 'SYNC/SAVE' : 'SAVE'}: ${durable}/${records.length} requested wave file(s) have been re-materialized; ${stats.pending} physical write job(s) remain queued and will retry automatically.`,
        'info'
      );
      renderAutoAuditState();
      return true;
    }

    setStatus(
      `${options.manualSync ? 'SYNC/SAVE' : 'SAVE'}: ${durable}/${records.length} COMPLETE wave file(s) physically re-materialized through AUDAPACK Bridge${allThree ? (combinedConfirmed ? ' · ALL_3 rebuilt/overwritten' : ' · all 3 waves written; bridge did not explicitly confirm ALL_3 in its response') : ''}.`,
      'success'
    );
    renderAutoAuditState();
    return true;
  }

  function setManualAuditSyncFeedback(value, duration = 3200) {
    manualAuditSyncFeedback = String(value || '');
    manualAuditSyncFeedbackUntil = manualAuditSyncFeedback
      ? Date.now() + Math.max(500, Number(duration) || 3200)
      : 0;

    if (manualAuditSyncFeedbackTimer) {
      clearTimeout(manualAuditSyncFeedbackTimer);
      manualAuditSyncFeedbackTimer = 0;
    }

    if (manualAuditSyncFeedbackUntil) {
      manualAuditSyncFeedbackTimer = setTimeout(() => {
        manualAuditSyncFeedbackTimer = 0;
        manualAuditSyncFeedback = '';
        manualAuditSyncFeedbackUntil = 0;
        renderAutoAuditState();
      }, Math.max(500, manualAuditSyncFeedbackUntil - Date.now()));
    }

    renderAutoAuditState();
  }

  async function syncSaveCurrentChatStateNow() {
    if (manualAuditSyncInFlight) {
      setStatus('SYNC/SAVE is already refreshing this chat.', 'info');
      return false;
    }

    manualAuditSyncInFlight = true;
    setManualAuditSyncFeedback('');
    renderAutoAuditState();

    try {
      bindAutoRuntimeToCurrentConversation({ claim: false });
      const conversationKey = autoBoundConversationKey || currentConversationKey();

      // Persist the exact current runtime snapshot first: stage, pause/hold
      // lineage, user anchors, project/run id, receipts and progress survive
      // reload even if disk persistence later fails.
      let runtimePersisted = true;
      if (autoRuntime) {
        autoRuntime.conversationKey = conversationKey;
        runtimePersisted = saveAutoRuntime({ pauseOnFailure: false });
      }

      const captured = backfillVisibleCompletedAuditResults();
      renderAutoAuditState();

      const beforeSave = currentChatAuditRecords(conversationKey);
      const result = await saveCurrentChatAuditsNow({
        forceAll: true,
        refreshVisible: false,
        manualSync: true
      });

      // Rebind/repaint after bridge work because ChatGPT may have hydrated a
      // turn or changed route while disk I/O was in flight.
      bindAutoRuntimeToCurrentConversation({ claim: false });
      backfillVisibleCompletedAuditResults();

      if (autoRuntime) {
        autoRuntime.conversationKey = autoBoundConversationKey || currentConversationKey();
        saveAutoRuntime({ pauseOnFailure: false });
      }

      const afterSave = currentChatAuditRecords();
      const allThree = afterSave.length === 3;
      const stats = state.bridgeEnabled ? bridgeQueueStats(autoBoundConversationKey || currentConversationKey()) : {
        pending: 0,
        failed: 0
      };

      renderAutoAuditState();

      if (!afterSave.length) {
        setStatus(
          `SYNC/SAVE: current runtime ${runtimePersisted ? 'persisted' : 'could not be durably persisted'}; no structurally COMPLETE wave exists yet. In-progress/partial assistant text was intentionally not written as a finished audit.`,
          runtimePersisted ? 'info' : 'warning'
        );
        setManualAuditSyncFeedback(runtimePersisted ? 'SYNCED' : 'SAVE!');
        return runtimePersisted;
      }

      if (stats.failed > 0) {
        setStatus(
          `SYNC/SAVE: refreshed current chat and found ${afterSave.length}/3 COMPLETE wave(s); ${stats.failed} bridge save job(s) failed physical-write verification. Nothing was discarded.`,
          'error'
        );
        setManualAuditSyncFeedback('SAVE!', 5000);
        return false;
      }

      if (stats.pending > 0) {
        setStatus(
          `SYNC/SAVE: refreshed current chat and found ${afterSave.length}/3 COMPLETE wave(s); ${stats.pending} physical write job(s) are still queued. Runtime state is persisted.`,
          'info'
        );
        setManualAuditSyncFeedback('QUEUE', 4200);
        return true;
      }

      setStatus(
        `SYNC/SAVE: refreshed current chat; physical rewrite completed for ${afterSave.length}/3 COMPLETE wave(s)${allThree ? ' plus ALL_3 rebuild' : ''}${captured ? ' · newly visible COMPLETE result(s) were captured first' : ''}. Runtime state is persisted.`,
        result === false ? 'warning' : 'success'
      );
      setManualAuditSyncFeedback(result === false ? 'SAVE!' : 'SAVED', result === false ? 5000 : 3200);
      return result !== false;
    } finally {
      manualAuditSyncInFlight = false;
      renderAutoAuditState();
    }
  }

  function currentAuditSaveAttention(jobsSnapshot = null, recordsSnapshot = null) {
    if (!state?.autoSaveAuditFiles) return false;
    if (state.bridgeEnabled) {
      const stats = currentBridgeSaveState(autoBoundConversationKey || currentConversationKey(), jobsSnapshot);
      return stats.pending > 0 || stats.failed > 0;
    }
    const results = Array.isArray(recordsSnapshot) ? recordsSnapshot : currentChatAuditRecords();
    if (!results.length) return false;
    return results.some(record => !record.savedAt) && auditDirectoryState !== 'ready';
  }

  function autoVisualPhase() {
    if (!autoRuntime) return 'idle';

    const stage = String(autoRuntime.stage || 'idle');
    if (stage === 'complete') return 'done';
    if (stage === 'paused') return 'attention';
    if (stage === 'wait-core') return 'core';

    if (['sending-second', 'await-second-user', 'wait-second'].includes(stage)) {
      return 'second';
    }

    if (['sending-performance', 'await-performance-user', 'wait-performance'].includes(stage)) {
      return 'performance';
    }

    if (['sending-continuation', 'await-continuation-user'].includes(stage)) {
      const kind = String(autoRuntime.continuationKind || '');
      if (kind === 'core') return 'core';
      if (kind === 'second') return 'second';
      if (kind === 'performance') return 'performance';
    }

    return 'idle';
  }

  function superCompactAutoLabel(jobsSnapshot = null, recordsSnapshot = null) {
    if (chatGPTRootIsQuarantined() || autoBoundConversationKey?.startsWith('auth:')) return 'AUTH';
    if (!autoRuntime) return 'INIT';
    if (!autoRuntime.enabled) return 'CHAT';

    const lease = readAutoLease(autoBoundConversationKey || currentConversationKey());
    if (lease && lease.ownerId && lease.ownerId !== autoInstanceId && lease.expiresAt > Date.now()) return 'STBY';
    if (autoRuntime.stage === 'paused') return 'PAUSE';
    if (autoComposerHoldReason) return 'HOLD';
    if (autoRuntime.stage === 'complete') {
      const bridgeSave = currentBridgeSaveState(autoBoundConversationKey || currentConversationKey(), jobsSnapshot);
      if (state?.bridgeEnabled && bridgeSave.failed > 0) return '!';
      if (currentAuditSaveAttention(jobsSnapshot, recordsSnapshot)) return 'SAVE';
      return 'DONE';
    }
    if (autoRuntime.anchorMissingSince) return 'VIRT';
    if (autoRuntime.continuationKind) return autoRuntime.continuationReason === 'stall' ? 'REC' : 'PART';
    if (chatGPTAuthInterstitialVisible()) return 'AUTH';
    const records = Array.isArray(recordsSnapshot) ? recordsSnapshot : currentChatAuditRecords();
    if (autoRuntime.stage === 'idle' && records.length) return 'REC';
    if (autoRuntime.stage === 'idle') return 'READY';
    if (autoRuntime.stage === 'wait-core') return 'CORE';
    if (['sending-second', 'await-second-user', 'wait-second'].includes(autoRuntime.stage)) return 'W2';
    if (['sending-performance', 'await-performance-user', 'wait-performance'].includes(autoRuntime.stage)) return 'PERF';
    return 'AUTO';
  }

  function renderAutoAuditState() {
    if (!panel || !state) return;

    const enabled = panel.querySelector('#acb-auto-enabled');
    const superEnabled = panel.querySelector('#acb-super-enabled');
    const superBrand = panel.querySelector('#acb-super-brand');
    const superStatus = panel.querySelector('#acb-super-state');
    const superProgress = panel.querySelector('#acb-super-progress');
    const renameToggle = panel.querySelector('#acb-auto-rename-chat');
    const saveNow = panel.querySelector('#acb-save-now');
    const bridgeToggle = panel.querySelector('#acb-bridge-enabled');
    const bridgeUrlInput = panel.querySelector('#acb-bridge-url');
    const strict = panel.querySelector('#acb-auto-gate');
    const delay = panel.querySelector('#acb-auto-delay');
    const timeout = panel.querySelector('#acb-auto-timeout');
    const delivery = panel.querySelector('#acb-prompt-delivery');
    const status = panel.querySelector('#acb-auto-state');
    const progress = panel.querySelector('#acb-auto-progress');
    const adopt = panel.querySelector('#acb-auto-adopt');
    const reset = panel.querySelector('#acb-auto-reset');
    const stop = panel.querySelector('#acb-auto-stop');
    const bridgeJobsSnapshot = listBridgeJobs();
    const auditRecordsSnapshot = currentChatAuditRecords();

    if (enabled) enabled.checked = Boolean(autoRuntime?.enabled);
    if (superEnabled) superEnabled.checked = Boolean(autoRuntime?.enabled);

    if (superBrand) {
      const identity = currentMiniIdentity();
      superBrand.textContent = identity.label;
      superBrand.dataset.identity = identity.kind;
      superBrand.title = identity.kind === 'project'
        ? `Current audit project: ${identity.label}`
        : identity.kind === 'chat-title'
          ? `Current chat: ${identity.label}`
          : 'No audit project detected in this chat.';
    }
    if (renameToggle) renameToggle.checked = Boolean(state.autoRenameChat);
    if (bridgeToggle) bridgeToggle.checked = Boolean(state.bridgeEnabled);
    if (bridgeUrlInput && document.activeElement !== bridgeUrlInput) bridgeUrlInput.value = state.bridgeUrl || BRIDGE_DEFAULT_URL;
    if (strict) strict.value = state.autoAuditStrictGate ? 'strict' : 'relaxed';
    if (delay) delay.value = String(state.autoAuditDelayMs);
    if (timeout) timeout.value = String(state.autoAuditTimeoutMin);
    if (delivery) delivery.value = state.chatgptPromptDelivery;

    const summary = autoStageSummary();
    if (status) {
      status.textContent = summary.text;
      status.dataset.kind = summary.kind;
      status.dataset.phase = autoVisualPhase();
      status.dataset.hold = autoComposerHoldReason ? 'true' : 'false';
      status.title = summary.text;
    }

    if (saveNow) {
      const stats = currentBridgeSaveState(autoBoundConversationKey || currentConversationKey(), bridgeJobsSnapshot);
      const records = auditRecordsSnapshot;
      const readyCount = records.length;
      const durableCount = records.filter(record => Number(record.bridgeSavedAt) > 0).length;
      saveNow.textContent = stats.failed > 0
        ? 'SAVE !'
        : stats.pending > 0
          ? 'SAVE'
          : readyCount > 0 && durableCount === readyCount
            ? 'SAVED'
            : 'SAVE';
      saveNow.dataset.state = stats.failed > 0
        ? 'error'
        : stats.pending > 0
          ? 'pending'
          : readyCount > 0 && durableCount === readyCount
            ? 'saved'
            : 'idle';
      saveNow.title = readyCount
        ? `SYNC/SAVE current chat: persist runtime, rescan visible COMPLETE waves, physically recreate/overwrite all ${readyCount} cached wave file(s), and rebuild ALL_3 when 3/3 exist. ${durableCount}/${readyCount} currently durable.`
        : 'SYNC/SAVE current chat: persist/refresh runtime state now. No structurally COMPLETE wave is cached yet.';
    }

    const progressSnapshot = autoProgressSnapshot();
    renderProgressContainer(progress, progressSnapshot);
    renderProgressContainer(superProgress, progressSnapshot);

    if (superStatus) {
      if (state.superCompact && autoRuntime?.stage === 'idle') {
        miniAttachmentSignature = composerAttachmentSignature();
      }

      const compactLabel = superCompactAutoLabel(bridgeJobsSnapshot, auditRecordsSnapshot);
      const start = miniStartAuditState();
      const showStart = start.available;
      const showStartBusy = start.busy;

      const activeSaveFeedback = manualAuditSyncFeedbackUntil > Date.now()
        ? manualAuditSyncFeedback
        : '';

      const visibleLabel = showStart
        ? 'START'
        : showStartBusy
          ? 'START...'
          : manualAuditSyncInFlight
            ? 'SAVE...'
            : activeSaveFeedback || compactLabel;

      superStatus.textContent = visibleLabel;
      superStatus.dataset.kind = summary.kind;
      superStatus.dataset.phase = autoVisualPhase();
      superStatus.dataset.hold = autoComposerHoldReason ? 'true' : 'false';
      superStatus.dataset.action = showStart
        ? 'start-audit'
        : 'sync-save';

      superStatus.dataset.state = showStart || showStartBusy ? 'start'
        : activeSaveFeedback === 'SAVED' ? 'done'
          : activeSaveFeedback === 'SAVE!' ? 'attention'
            : activeSaveFeedback === 'QUEUE' ? 'recover'
              : compactLabel === 'DONE' ? 'done'
                : compactLabel === 'PAUSE' || compactLabel === '!' ? 'attention'
                  : compactLabel === 'SAVE' ? 'recover'
                    : ['REC', 'PART', 'VIRT', 'HOLD', 'AUTH'].includes(compactLabel) ? 'recover'
                      : compactLabel === 'STBY' ? 'standby'
                        : ['CORE', 'W2', 'PERF', 'AUTO'].includes(compactLabel) ? 'running'
                          : 'idle';

      superStatus.disabled = showStartBusy || manualAuditSyncInFlight;

      const bridgeSave = currentBridgeSaveState(autoBoundConversationKey || currentConversationKey(), bridgeJobsSnapshot);
      const saveNote = state?.bridgeEnabled
        ? bridgeSave.failed > 0
          ? ` ${bridgeSave.failed} disk-save job(s) require bridge/token/config attention; audit text remains cached.`
          : bridgeSave.pending > 0
            ? ` ${bridgeSave.pending} disk-save job(s) are queued for AUDAPACK Bridge.`
            : ''
        : currentAuditSaveAttention(bridgeJobsSnapshot, auditRecordsSnapshot)
          ? ' Completed audit is cached, but browser-folder saving needs attention.'
          : '';

      if (showStart) {
        const names = start.attachment?.names?.slice(0, 3).join(', ') || 'attached project';
        const extra = Number(start.attachment?.count || 0) > 3
          ? ` +${start.attachment.count - 3} more`
          : '';
        superStatus.title = start.retryPrepared
          ? 'START AUDITING · retry the already prepared canonical Core receipt. No second Core/receipt will be created.'
          : `START AUDITING · send canonical Audit Core now using the attached project file(s): ${names}${extra}. Auto3 continues Core -> Second -> Performance.`;
      } else if (showStartBusy) {
        superStatus.title = 'START AUDITING is preparing/sending Audit Core. Duplicate clicks are blocked.';
      } else {
        const attachment = ['READY', 'CHAT'].includes(compactLabel) ? chatGPTReadyAttachmentSummary() : null;
        const startHint = ['READY', 'CHAT'].includes(compactLabel) && attachment?.reason
          ? ` ${attachment.reason}`
          : '';

        superStatus.title = `${summary.text}${autoRuntime?.projectName ? ` Project: ${autoRuntime.projectName}.` : ''}${saveNote}${startHint} Click to SYNC/SAVE this chat: persist current runtime state, rescan COMPLETE waves, physically recreate/overwrite every available audit file through AUDAPACK Bridge, and rebuild ALL_3 when all three waves exist. In-progress assistant text is never mislabeled as COMPLETE.`;
      }
    }
    renderAuditFolderState();
    renderBridgeState(bridgeJobsSnapshot);

    const chatgpt = detectSite().key === 'chatgpt';
    if (delivery) delivery.disabled = !chatgpt;
    if (adopt) adopt.disabled = !autoRuntime?.enabled || !chatgpt;
    if (reset) reset.disabled = !chatgpt;
    if (stop) stop.disabled = !autoRuntime?.enabled || !chatgpt || autoRuntime?.stage === 'idle' || autoRuntime?.stage === 'complete';
  }

  function pauseAutoAudit(reason, kind = 'warning') {
    if (!autoRuntime) autoRuntime = emptyAutoRuntime();

    if (!pauseIsExplicitHumanStop(reason)) {
      const tx = readCommittedAutoSend();
      const receipt = String(tx?.receipt || autoRuntime.pendingSendReceipt || '');
      const turn = exactReceiptUserTurn(receipt, getChatGPTTurns());
      const expectedKind = String(tx?.kind || autoRuntime.pendingSendKind || autoRuntime.expectedKind || (turn ? classifyAuditTurn(turn) : ''));
      if (turn || tx?.phase === 'clicked') {
        const awaitStage = autoAwaitStageForKind(expectedKind, Boolean(tx?.continuation || autoRuntime.continuationKind));
        if (awaitStage) {
          autoRuntime.stage = awaitStage;
          autoRuntime.expectedKind = expectedKind;
          autoRuntime.pausedReason = '';
          autoRuntime.pausedFromStage = '';
          autoRuntime.waitStartedAt = Date.now();
          saveAutoRuntime({ pauseOnFailure: false });
          ensureAutoAuditObserver();
          setStatus(turn ? `${waveLabel(expectedKind)} receipt is already visible. Stale transient PAUSE ignored; Auto3 resumed.` : `${waveLabel(expectedKind)} Send click is committed. Transient PAUSE ignored while ChatGPT registers the turn.`, 'success');
          scheduleAutoAuditCheck(500);
          return;
        }
      }
    }

    clearAutoComposerHold();
    clearAutoTimers();
    if (autoRuntime.stage !== 'paused') autoRuntime.pausedFromStage = autoRuntime.stage;
    autoRuntime.stage = 'paused';
    autoRuntime.pausedReason = reason;
    autoRuntime.waitStartedAt = 0;
    autoRuntime.stableResponseKey = '';
    autoRuntime.stableSince = 0;
    ensureAutoAuditObserver();
    const persisted = saveAutoRuntime({ pauseOnFailure: false });
    if (pauseIsExplicitHumanStop(reason)) {
      // A deliberate pause has no automatic work to protect. Holding the lease
      // forever would make Resume in another tab impossible while this tab stays
      // open, so release ownership after persisting the shared paused state.
      releaseAutoLease(autoBoundConversationKey);
    }
    setStatus(
      persisted ? `Auto audit paused: ${reason}` : `Auto audit paused in memory but could not persist the pause: ${reason}`,
      persisted ? kind : 'error'
    );
  }

  function resetAutoAuditRuntime(options = {}) {
    clearAutoComposerHold();
    clearAutoTimers();
    bindAutoRuntimeToCurrentConversation({ claim: false });

    const previousRuntime = autoRuntime;
    const enabled = Boolean(autoRuntime?.enabled && !autoRuntime?.storageCorrupt);
    const conversationKey = autoBoundConversationKey || currentConversationKey();
    const turns = getChatGPTTurns();
    const latestUser = latestChatGPTUserTurn(turns);
    const latestAssistant = latestUser ? assistantTurnAfter(latestUser, turns) : null;

    const replacement = emptyAutoRuntime({ enabled });
    replacement.conversationKey = conversationKey;
    replacement.seenUserId = getTurnId(latestUser);
    replacement.baselineAssistantKey = assistantFingerprint(latestAssistant);
    replacement.resetBarrierActive = true;
    replacement.resetBarrierUserId = getTurnId(latestUser);

    // Durable reset barrier first. Session checkpoints and A3 intent stay intact
    // until this verified write succeeds, so a failed Reset cannot half-destroy
    // the previous recoverable chain.
    if (!persistRuntimeForKey(conversationKey, replacement)) {
      autoRuntime = previousRuntime;
      setStatus('Reset failed before the durable barrier committed. Previous START/send/runtime checkpoints were preserved.', 'error');
      renderAutoAuditState();
      return false;
    }

    autoRuntimeCorruptKey = '';
    autoRuntime = replacement;
    clearStartAuditHandoff();
    clearCommittedAutoSend();
    if (enabled) writeA3Intent(true, conversationKey, { startTransaction: false });
    else clearA3Intent();
    ensureAutoAuditObserver();

    const physicallyCleared = clearAuditResultsForConversation(conversationKey);
    if (enabled) claimAutoLease();
    renderAutoAuditState();

    if (!options.silent) {
      setStatus(
        physicallyCleared
          ? 'Reset this conversation\'s Auto3 chain and cached audit lineage. Waiting for a fresh Core/START.'
          : 'Reset barrier committed. Some old cache keys could not be physically deleted, but the barrier logically fences them and they will not be shown/saved/adopted.',
        physicallyCleared ? 'success' : 'warning'
      );
    }
    return true;
  }

  function stageTimedOut() {
    if (!autoRuntime?.waitStartedAt) return false;
    const maxMs = state.autoAuditTimeoutMin * 60 * 1000;
    return Date.now() - autoRuntime.waitStartedAt > maxMs;
  }

  function reconcileExecutionGap(now = Date.now()) {
    const current = Math.max(0, Number(now) || Date.now());
    const previous = Math.max(0, Number(autoLastEvaluationAt) || 0);
    autoLastEvaluationAt = current;

    if (!previous || current <= previous) return false;
    const gap = current - previous;
    if (gap <= AUTO_EXECUTION_GAP_RESET_MS || !autoRuntime?.enabled) return false;
    if (autoRuntime.stage === 'paused' || autoRuntime.stage === 'complete') return false;

    // A long JavaScript execution gap means the renderer was likely frozen,
    // background-throttled, BFCache-suspended, or the machine/display session
    // stopped scheduling this page. Wall-clock silence during that gap is not
    // evidence that the assistant stalled. Restart every DOM-stability/liveness
    // grace window and shift absolute wait clocks so wake-up cannot instantly
    // emit a stale CONTINUE or timeout before queued DOM/network work hydrates.
    const shiftClock = value => {
      const parsed = Math.max(0, Number(value) || 0);
      return parsed ? Math.min(current, parsed + gap) : 0;
    };

    autoRuntime.waitStartedAt = shiftClock(autoRuntime.waitStartedAt);
    autoRuntime.pendingSendStartedAt = shiftClock(autoRuntime.pendingSendStartedAt);
    autoRuntime.idleStallKey = '';
    autoRuntime.idleStallSince = 0;
    autoRuntime.stableResponseKey = '';
    autoRuntime.stableSince = 0;
    autoRuntime.anchorMissingSince = 0;

    saveAutoRuntime({ pauseOnFailure: false });
    return true;
  }

  function ensureAutoConversation(turns) {
    bindAutoRuntimeToCurrentConversation({ claim: false });
    if (!autoRuntime || !autoRuntime.enabled) {
      renderAutoAuditState();
      return false;
    }

    if (autoRuntime.stage === 'paused' && pauseIsExplicitHumanStop(autoRuntime.pausedReason)) {
      releaseAutoLease(autoBoundConversationKey);
      renderAutoAuditState();
      return true;
    }

    const ownedBeforeClaim = autoLeaseOwnedByThisTab(autoBoundConversationKey);
    if (claimAutoLease()) {
      // Ownership was just (re)acquired. Adopt the latest persisted runtime and
      // invalidate full-text result cache so a standby/frozen tab cannot resume
      // from values written before the previous owner completed more work.
      if (!ownedBeforeClaim) invalidateAuditResultCache(autoBoundConversationKey);
      refreshAutoRuntimeFromStorage();
      return true;
    }

    refreshAutoRuntimeFromStorage();
    renderAutoAuditState();

    const lease = readAutoLease(autoBoundConversationKey);
    const wait = lease?.expiresAt > Date.now()
      ? Math.min(5000, Math.max(900, lease.expiresAt - Date.now() + 120))
      : 900;
    scheduleAutoAuditCheck(wait);
    return false;
  }

  function completedAssistantCandidate(turn, stage = autoRuntime?.stage || '') {
    if (!turn) return { complete: false, reason: 'no-assistant' };
    if (chatGPTIsGenerating()) return { complete: false, reason: 'generating' };
    if (assistantNeedsContinuation(turn)) return { complete: false, reason: 'continue-generating' };
    if (assistantHasRetryError(turn)) return { complete: false, reason: 'retry-error' };

    // One bounded extraction pass feeds the gate, the fallback text and the
    // fingerprint, so a stabilization evaluation never re-reads the answer.
    const snapshot = buildAssistantSnapshot(turn);
    const gate = responseGateFromAssistantTurn(stage, turn, snapshot);
    const text = gate.text || snapshot.candidates[0] || '';
    if (!text) return { complete: false, reason: 'empty' };

    // Response-action buttons are useful but not authoritative after reload:
    // ChatGPT can lazy-render them. A structurally terminal audit handoff is
    // sufficient when generation has stopped.
    const hasUiFinality = assistantHasFinalActions(turn);
    const hasAuditFinality = gate.state !== 'unknown';
    if (!hasUiFinality && !hasAuditFinality) {
      return { complete: false, reason: 'no-finality-evidence' };
    }

    const key = assistantFingerprint(turn, snapshot);
    const now = Date.now();

    if (autoRuntime.stableResponseKey !== key) {
      autoRuntime.stableResponseKey = key;
      autoRuntime.stableSince = now;
      saveAutoRuntime();
      scheduleAutoAuditCheck(AUTO_RESPONSE_STABLE_MS + 80);
      return { complete: false, reason: 'stabilizing' };
    }

    if (now - autoRuntime.stableSince < AUTO_RESPONSE_STABLE_MS) {
      scheduleAutoAuditCheck(AUTO_RESPONSE_STABLE_MS - (now - autoRuntime.stableSince) + 80);
      return { complete: false, reason: 'stabilizing' };
    }

    return {
      complete: true,
      text,
      key,
      gate: gate.state,
      sourceCount: gate.sourceCount
    };
  }

  function armFromCoreTurn(userTurn, options = {}) {
    if (!userTurn || classifyAuditTurn(userTurn) !== 'core') return false;

    // A CORE CONTINUE is same-wave recovery evidence, never a fresh audit root.
    // Treating it as a new Core after reload/COMPLETE resets runId/cache/project
    // and can restart the entire 3-wave chain from a stale recovery message.
    if (auditTurnIsContinuation(userTurn)) return false;

    const turns = getChatGPTTurns();
    const assistant = assistantTurnAfter(userTurn, turns);
    const finalAlready = assistant && !chatGPTIsGenerating() && assistantHasFinalActions(assistant);

    if (finalAlready && !options.allowCompleted) return false;

    clearAutoTimers();

    // START already resolved the project from the composer attachment before Send.
    // The authored Core turn can hydrate in pieces: its text/receipt may exist before
    // attachment tiles expose their filenames. Never erase the authoritative START
    // snapshot merely because projectNameFromCoreTurn() is temporarily empty.
    const pendingStart = readStartAuditHandoff();
    const startReceiptMatches = Boolean(
      pendingStart?.receipt && userTurnContainsReceipt(userTurn, pendingStart.receipt)
    );
    const inferredProjectName = sanitizeProjectIdentity(projectNameFromCoreTurn(userTurn));
    const startProjectName = startReceiptMatches
      ? sanitizeProjectIdentity(pendingStart?.runtime?.projectName || '')
      : '';
    const selectedProjectName = inferredProjectName || startProjectName;
    const selectedProjectSource = inferredProjectName
      ? 'artifact'
      : startProjectName
        ? String(pendingStart?.runtime?.projectNameSource || 'artifact')
        : '';

    autoRuntime.projectName = selectedProjectName;
    autoRuntime.projectNameSource = selectedProjectSource;
    autoRuntime.renameAppliedName = '';
    autoRuntime.renamePersistedName = '';
    autoRuntime.renamePersistedAt = 0;
    autoRuntime.renameAttemptName = '';
    autoRuntime.renameAttemptCount = 0;
    autoRuntime.stage = 'wait-core';
    autoRuntime.conversationKey = currentConversationKey();
    autoRuntime.anchorUserId = getTurnId(userTurn);
    autoRuntime.coreUserId = autoRuntime.anchorUserId;
    autoRuntime.secondUserId = '';
    autoRuntime.performanceUserId = '';
    autoRuntime.expectedKind = '';
    autoRuntime.runId = createAuditRunId();
    autoRuntime.startedAt = Date.now();
    autoRuntime.waitStartedAt = Date.now();
    autoRuntime.stableResponseKey = '';
    autoRuntime.stableSince = 0;
    autoRuntime.stageAssistantId = '';
    autoRuntime.anchorMissingSince = 0;
    autoRuntime.pausedReason = '';
    autoRuntime.resetBarrierActive = false;
    autoRuntime.resetBarrierUserId = '';
    if (!saveAutoRuntime()) return false;

    // Only discard the previous run's cached handoffs after the new Core runtime
    // checkpoint is durable. Storage failure must not destroy the last known-good
    // audit evidence while also failing to arm the replacement run.
    clearAuditResultsForConversation(currentConversationKey());

    if (startHandoffIsCommitted(pendingStart)) clearStartAuditHandoff();

    setStatus(`Auto audit armed from the latest AUDIT CORE turn${autoRuntime.projectName ? ` for ${autoRuntime.projectName}` : ''}. Waiting for Core to finish before sending Second Wave.`, 'success');
    if (autoRuntime.projectName) {
      const renameContext = {
        source: 'artifact',
        conversationKey: currentConversationKey(),
        runStartedAt: autoRuntime.startedAt
      };
      maybeRenameConversation(autoRuntime.projectName, renameContext).catch(() => { });
      scheduleConversationTitleGuard(autoRuntime.projectName, renameContext);
    }
    scheduleAutoAuditCheck(250);
    return true;
  }

  function previousAuditUserTurn(turn, wantedKind, turns = getChatGPTTurns()) {
    const start = turns.indexOf(turn);
    if (start < 0) return null;
    for (let index = start - 1; index >= 0; index -= 1) {
      if (turnRole(turns[index]) !== 'user') continue;
      const kind = classifyAuditTurn(turns[index]);
      if (!kind) return null; // non-audit user turn is a hard lineage barrier
      return kind === wantedKind ? turns[index] : null;
    }
    return null;
  }

  function recoverExpectedStageAnchor(stage, turns = getChatGPTTurns()) {
    const expected = stage === 'wait-core'
      ? 'core'
      : stage === 'wait-second'
        ? 'second'
        : stage === 'wait-performance'
          ? 'performance'
          : '';
    if (!expected) return null;

    const latestUser = latestExpectedAuditUserTurn(expected, turns);
    if (!latestUser) return null;

    const id = getTurnId(latestUser);
    if (id) {
      if (expected === 'core') autoRuntime.coreUserId = id;
      if (expected === 'second') autoRuntime.secondUserId = id;
      if (expected === 'performance') autoRuntime.performanceUserId = id;
      autoRuntime.anchorUserId = autoRuntime.anchorUserId || id;
      autoRuntime.seenUserId = id;
      autoRuntime.conversationKey = currentConversationKey();
      autoRuntime.anchorMissingSince = 0;
      if (!saveAutoRuntime()) return null;
    } else {
      autoRuntime.anchorMissingSince = 0;
    }
    return latestUser;
  }

  function adoptCurrentAuditTurn() {
    const turns = getChatGPTTurns();
    const lineage = visibleAuditLineage(turns);
    const latestUser = lineage.blockedByReset
      ? null
      : (lineage.performance || lineage.second || lineage.core);
    const kind = classifyAuditTurn(latestUser);

    if (!latestUser || !kind) {
      setStatus('Adopt failed: no recognizable AUDIT CORE / SECOND WAVE / PERFORMANCE command is available in the current conversation.', 'warning');
      return false;
    }

    const resumed = resumeRuntimeFromAuditTurn(latestUser, { turns });
    if (!resumed) {
      setStatus(
        kind === 'core'
          ? 'Adopt failed: the Core turn does not have a stable turn identity yet.'
          : kind === 'second'
            ? 'Adopt failed: no valid earlier AUDIT CORE lineage exists before this Second Wave.'
            : 'Adopt failed: the Performance turn does not have a valid earlier Core -> Second Wave lineage.',
        'warning'
      );
    }
    return resumed;
  }

  function readCommittedAutoSend() {
    try {
      const raw = sessionStorage.getItem(AUTO_COMMITTED_SEND_SESSION_KEY);
      if (!raw) return null;
      const parsed = JSON.parse(raw);
      if (!parsed || parsed.version !== 1 || parsed.tabId !== autoTabId) return null;
      if (!Number(parsed.expiresAt) || parsed.expiresAt <= Date.now()) {
        sessionStorage.removeItem(AUTO_COMMITTED_SEND_SESSION_KEY);
        return null;
      }
      return parsed;
    } catch (_) { return null; }
  }

  function writeCommittedAutoSend(tx) {
    if (!tx) return false;
    try { sessionStorage.setItem(AUTO_COMMITTED_SEND_SESSION_KEY, JSON.stringify(tx)); return true; }
    catch (_) { return false; }
  }

  function clearCommittedAutoSend(expectedReceipt = '') {
    const current = readCommittedAutoSend();
    if (expectedReceipt && current?.receipt && current.receipt !== expectedReceipt) return;
    try { sessionStorage.removeItem(AUTO_COMMITTED_SEND_SESSION_KEY); } catch (_) { }
  }

  function autoAwaitStageForKind(kind, continuation = false) {
    if (continuation) return 'await-continuation-user';
    if (kind === 'second') return 'await-second-user';
    if (kind === 'performance') return 'await-performance-user';
    return '';
  }

  function beginCommittedAutoSend(kind, receipt, options = {}) {
    if (!kind || !receipt) return null;
    const now = Date.now();
    const tx = {
      version: 1, tabId: autoTabId,
      conversationKey: autoBoundConversationKey || currentConversationKey(),
      runId: String(autoRuntime?.runId || ''),
      kind, receipt,
      continuation: Boolean(options.continuation),
      previousUserId: String(options.previousUserId || ''),
      phase: 'prepared', preparedAt: now, clickedAt: 0,
      expiresAt: now + AUTO_COMMITTED_SEND_TTL_MS
    };
    writeCommittedAutoSend(tx);
    return tx;
  }

  function markCommittedAutoSendClicked(kind, receipt, options = {}) {
    const current = readCommittedAutoSend();
    const now = Date.now();
    const tx = current && current.receipt === receipt ? current : beginCommittedAutoSend(kind, receipt, options);
    if (!tx) return null;
    tx.phase = 'clicked';
    tx.clickedAt = now;
    tx.expiresAt = now + AUTO_COMMITTED_SEND_TTL_MS;
    tx.conversationKey = autoBoundConversationKey || currentConversationKey();
    tx.runId = String(autoRuntime?.runId || tx.runId || '');
    writeCommittedAutoSend(tx);
    return tx;
  }

  function exactReceiptUserTurn(receipt, turns = getChatGPTTurns()) {
    if (!receipt) return null;
    for (let i = turns.length - 1; i >= 0; i -= 1) if (userTurnContainsReceipt(turns[i], receipt)) return turns[i];
    return null;
  }

  function pauseIsExplicitHumanStop(reason = '') {
    return /Paused manually from the widget|reported BLOCKED|hard audit precondition failure/i.test(String(reason || ''));
  }

  function recoverCommittedSendFromDom(turns = getChatGPTTurns()) {
    if (!autoRuntime?.enabled) return false;
    const tx = readCommittedAutoSend();
    const receipt = String(tx?.receipt || autoRuntime.pendingSendReceipt || '');
    const turn = exactReceiptUserTurn(receipt, turns);
    const kind = String(tx?.kind || autoRuntime.pendingSendKind || autoRuntime.expectedKind || autoRuntime.continuationKind || (turn ? classifyAuditTurn(turn) : ''));

    if (turn && kind && classifyAuditTurn(turn) === kind) {
      const id = getTurnId(turn);
      if (!id) {
        autoRuntime.stage = autoAwaitStageForKind(kind, Boolean(tx?.continuation || autoRuntime.continuationKind)) || autoRuntime.stage;
        autoRuntime.expectedKind = kind;
        autoRuntime.pausedReason = '';
        autoRuntime.pausedFromStage = '';
        saveAutoRuntime({ pauseOnFailure: false });
        setStatus(`${waveLabel(kind)} Send receipt is already visible. Waiting only for turn-id hydration; no Resume and no duplicate Send.`, 'success');
        scheduleAutoAuditCheck(700);
        return true;
      }
      setWaveUserId(kind, id);
      autoRuntime.seenUserId = id;
      autoRuntime.stage = waveWaitStage(kind);
      autoRuntime.expectedKind = '';
      autoRuntime.continuationKind = '';
      autoRuntime.continuationReason = '';
      autoRuntime.continuationPreviousUserId = '';
      autoRuntime.pausedReason = '';
      autoRuntime.pausedFromStage = '';
      autoRuntime.waitStartedAt = Date.now();
      autoRuntime.stableResponseKey = '';
      autoRuntime.stableSince = 0;
      autoRuntime.stageAssistantId = '';
      autoRuntime.anchorMissingSince = 0;
      autoRuntime.idleStallKey = '';
      autoRuntime.idleStallSince = 0;
      clearPendingSendReceipt({ save: false });
      clearCommittedAutoSend(receipt);
      saveAutoRuntime({ pauseOnFailure: false });
      setStatus(`${waveLabel(kind)} committed Send recovered from exact receipt. Auto3 resumed automatically.`, 'success');
      scheduleAutoAuditCheck(350);
      return true;
    }

    if (tx?.phase === 'clicked' && autoRuntime.stage === 'paused' && !pauseIsExplicitHumanStop(autoRuntime.pausedReason)) {
      autoRuntime.stage = autoAwaitStageForKind(tx.kind, tx.continuation);
      autoRuntime.expectedKind = tx.kind;
      autoRuntime.pendingSendReceipt = tx.receipt;
      autoRuntime.pendingSendKind = tx.kind;
      autoRuntime.pendingSendPreviousUserId = autoRuntime.pendingSendPreviousUserId || tx.previousUserId || '';
      autoRuntime.pendingSendClickArmed = true;
      autoRuntime.pendingSendStartedAt = Number(tx.clickedAt) || Date.now();
      autoRuntime.waitStartedAt = Date.now();
      autoRuntime.pausedReason = '';
      autoRuntime.pausedFromStage = '';
      saveAutoRuntime({ pauseOnFailure: false });
      setStatus(`${waveLabel(tx.kind)} Send was already clicked. Transient PAUSE absorbed; waiting for ChatGPT registration without resending.`, 'success');
      scheduleAutoAuditCheck(700);
      return true;
    }
    return false;
  }

  function createAutoSendReceipt(kind) {
    const salt = globalThis.crypto && typeof globalThis.crypto.randomUUID === 'function'
      ? globalThis.crypto.randomUUID().replace(/-/g, '').slice(0, 12)
      : Math.random().toString(36).slice(2, 14);
    return `${kind}-${Date.now().toString(36)}-${salt}`;
  }

  function clearPendingSendReceipt(options = {}) {
    if (!autoRuntime) return;
    autoRuntime.pendingSendReceipt = '';
    autoRuntime.pendingSendKind = '';
    autoRuntime.pendingSendPreviousUserId = '';
    autoRuntime.pendingSendStartedAt = 0;
    autoRuntime.pendingSendRetries = 0;
    autoRuntime.pendingSendClickArmed = false;
    if (options.save !== false) saveAutoRuntime();
  }

  function ensurePendingSendReceipt(kind, previousUserId = '') {
    if (!autoRuntime) return '';

    if (autoRuntime.pendingSendReceipt && autoRuntime.pendingSendKind === kind) {
      if (previousUserId && !autoRuntime.pendingSendPreviousUserId) {
        autoRuntime.pendingSendPreviousUserId = previousUserId;
      }
      return autoRuntime.pendingSendReceipt;
    }

    autoRuntime.pendingSendReceipt = createAutoSendReceipt(kind);
    autoRuntime.pendingSendKind = kind;
    autoRuntime.pendingSendPreviousUserId = previousUserId || '';
    autoRuntime.pendingSendStartedAt = Date.now();
    autoRuntime.pendingSendRetries = 0;
    autoRuntime.pendingSendClickArmed = false;
    if (!saveAutoRuntime()) return '';
    return autoRuntime.pendingSendReceipt;
  }

  function userTurnContainsReceipt(turn, receipt) {
    if (!turn || !receipt || turnRole(turn) !== 'user') return false;
    const needle = `${AUTO_SEND_RECEIPT_PREFIX}: ${receipt}`;
    return getTurnText(turn).includes(needle) || readableNodeText(turn).includes(needle);
  }

  function findPendingSentAuditTurn(expectedKind, turns = getChatGPTTurns()) {
    const receipt = String(autoRuntime?.pendingSendReceipt || '');
    const previousId = String(
      autoRuntime?.pendingSendPreviousUserId ||
      (expectedKind === 'second' ? autoRuntime?.coreUserId : expectedKind === 'performance' ? autoRuntime?.secondUserId : autoRuntime?.continuationPreviousUserId) || ''
    );

    if (receipt) {
      for (let index = turns.length - 1; index >= 0; index -= 1) {
        if (userTurnContainsReceipt(turns[index], receipt)) return turns[index];
      }
      return null;
    }

    const previous = previousId ? findTurnById(previousId, turns) : null;
    const previousIndex = previous ? turns.indexOf(previous) : -1;
    if (previousId && previousIndex < 0) return null; // virtualized legacy anchor is ambiguous: fail closed.

    if (previousIndex >= 0) {
      let matched = null;
      for (let index = previousIndex + 1; index < turns.length; index += 1) {
        const candidate = turns[index];
        if (turnRole(candidate) !== 'user') continue;
        const kind = classifyAuditTurn(candidate);
        if (kind === 'core' && !auditTurnIsContinuation(candidate)) break;
        if (kind === expectedKind) matched = candidate;
      }
      return matched;
    }

    const lineage = visibleAuditLineage(turns);
    if (lineage.blockedByReset) return null;
    if (expectedKind === 'core') return lineage.core;
    if (expectedKind === 'second') return lineage.second;
    if (expectedKind === 'performance') return lineage.performance;
    return null;
  }

  function adoptRegisteredAutoSendTurn(expectedKind, turn) {
    if (!autoRuntime || !turn) return false;
    const id = getTurnId(turn);

    if (!id) {
      if (autoRuntime.stage === 'paused' && !pauseIsExplicitHumanStop(autoRuntime.pausedReason)) {
        const tx = readCommittedAutoSend();
        autoRuntime.stage = autoAwaitStageForKind(expectedKind, Boolean(tx?.continuation || autoRuntime.continuationKind)) || autoRuntime.stage;
        autoRuntime.pausedReason = '';
        autoRuntime.pausedFromStage = '';
        saveAutoRuntime({ pauseOnFailure: false });
      }
      setStatus(
        `${waveLabel(expectedKind)} Send is visibly present, but ChatGPT has not hydrated a stable turn id yet. Waiting; no duplicate and no human Resume.`,
        'info'
      );
      scheduleAutoAuditCheck(AUTO_LIVENESS_CHECK_MS);
      return true;
    }

    setWaveUserId(expectedKind, id);
    autoRuntime.seenUserId = id;
    autoRuntime.stage = waveWaitStage(expectedKind);
    autoRuntime.expectedKind = '';
    autoRuntime.continuationKind = '';
    autoRuntime.continuationReason = '';
    autoRuntime.continuationPreviousUserId = '';
    autoRuntime.waitStartedAt = Date.now();
    autoRuntime.stableResponseKey = '';
    autoRuntime.stableSince = 0;
    autoRuntime.stageAssistantId = '';
    autoRuntime.anchorMissingSince = 0;
    autoRuntime.idleStallKey = '';
    autoRuntime.idleStallSince = 0;
    const committedReceipt = String(autoRuntime.pendingSendReceipt || '');
    clearPendingSendReceipt({ save: false });
    clearCommittedAutoSend(committedReceipt);
    if (!saveAutoRuntime({ pauseOnFailure: false })) {
      setStatus(`${waveLabel(expectedKind)} turn registered; runtime storage is temporarily unavailable, but the visible turn will be re-adopted automatically.`, 'warning');
      scheduleAutoAuditCheck(1200);
      return true;
    }

    setStatus(`${waveLabel(expectedKind)} user turn registered. Waiting for its assistant result.`, 'success');
    scheduleAutoAuditCheck(500);
    return true;
  }

  function recoverPendingSendRegistration(expectedKind, turns = getChatGPTTurns()) {
    const candidate = findPendingSentAuditTurn(expectedKind, turns);
    return candidate ? adoptRegisteredAutoSendTurn(expectedKind, candidate) : false;
  }

  function composerContainsPendingReceipt(receipt) {
    if (!receipt) return false;
    const site = detectSite();
    if (site.key !== 'chatgpt') return false;
    const input = cachedSiteElement(site, 'input');
    if (!input) return false;
    const contains = composerPlainText(input).includes(`${AUTO_SEND_RECEIPT_PREFIX}: ${receipt}`);
    if (!contains) return false;
    const send = cachedSiteElement(site, 'send');
    return Boolean(send && !send.disabled && send.getAttribute('aria-disabled') !== 'true');
  }

  async function retryPendingComposerSendClick(expectedKind) {
    if (!autoRuntime?.enabled || !expectedKind) return 'not-applicable';
    const receipt = String(autoRuntime.pendingSendReceipt || '');
    if (!receipt) return 'not-applicable';
    if (chatGPTComposerReceiptState(receipt) !== 'present-with-receipt') return 'not-prepared';

    const retries = Math.max(0, Number(autoRuntime.pendingSendRetries) || 0);
    if (retries >= AUTO_MAX_SEND_REGISTRATION_RETRIES) return 'retry-exhausted';
    const token = await verifyAutoLeaseForSend();
    if (!token) { scheduleAutoAuditCheck(900); return 'ownership-lost'; }
    const site = detectSite();
    const send = cachedSiteElement(site, 'send');
    if (!send || send.disabled || send.getAttribute('aria-disabled') === 'true') {
      scheduleAutoAuditCheck(900);
      return 'send-not-ready';
    }

    const continuation = Boolean(autoRuntime.continuationKind || autoRuntime.stage === 'await-continuation-user');
    const awaitStage = autoAwaitStageForKind(expectedKind, continuation);
    if (!awaitStage) return 'not-applicable';
    const initial = chatGPTComposerStateSnapshot();
    if (!initial) return 'not-prepared';
    const ownership = createAutoSendOwnershipGuard(token, initial);
    ownership.captureWrite();

    autoRuntime.pendingSendRetries = retries + 1;
    autoRuntime.pendingSendStartedAt = Date.now();
    autoRuntime.pendingSendClickArmed = true;
    autoRuntime.stage = awaitStage;
    autoRuntime.expectedKind = expectedKind;
    if (!autoRuntime.waitStartedAt) autoRuntime.waitStartedAt = Date.now();
    if (!saveAutoRuntime({ pauseOnFailure: true })) return 'persistence-failed';
    if (!(await ownership.verify())) return 'ownership-lost';

    beginCommittedAutoSend(expectedKind, receipt, { continuation, previousUserId: autoRuntime.pendingSendPreviousUserId });
    const accepted = await clickChatGPTSendVerified(send, rawChatGPTComposerInput(), { receipt, autoOwnership: ownership });
    if (!accepted) {
      setStatus(`${waveLabel(expectedKind)} receipt is still prepared after click recovery. No duplicate text was inserted; bounded registration recovery remains active.`, 'warning');
      scheduleAutoAuditCheck(900);
      return 'still-prepared';
    }
    markCommittedAutoSendClicked(expectedKind, receipt, { continuation, previousUserId: autoRuntime.pendingSendPreviousUserId });
    setStatus(`${waveLabel(expectedKind)} pending receipt was submitted on click-only recovery (${autoRuntime.pendingSendRetries}/${AUTO_MAX_SEND_REGISTRATION_RETRIES}).`, 'warning');
    scheduleAutoAuditCheck(700);
    return 'accepted';
  }

  function scheduleRegistrationRecovery(expectedKind) {
    if (!autoRuntime) return;
    const origin = Number(autoRuntime.waitStartedAt) || Number(autoRuntime.pendingSendStartedAt) || Date.now();
    const totalElapsed = Date.now() - origin;
    const receipt = String(autoRuntime.pendingSendReceipt || '');
    const receiptState = receipt ? chatGPTComposerReceiptState(receipt) : 'no-receipt';

    if (totalElapsed >= AUTO_SEND_REGISTER_HARD_TIMEOUT_MS) {
      const priorStage = autoRuntime.stage;
      clearCommittedAutoSend(receipt);
      autoRuntime.stage = 'paused';
      autoRuntime.pausedFromStage = priorStage;
      autoRuntime.pausedReason = `Send registration remained ambiguous for ${Math.round(AUTO_SEND_REGISTER_HARD_TIMEOUT_MS / 1000)} seconds. The exact receipt was neither safely registered nor provably resolved; automatic re-send is stopped to prevent duplication.`;
      autoRuntime.waitStartedAt = 0;
      saveAutoRuntime({ pauseOnFailure: false });
      setStatus(`${waveLabel(expectedKind)} send registration reached its bounded safety horizon. Auto3 failed closed instead of waiting or clicking forever.`, 'warning');
      return;
    }

    const retryElapsed = Date.now() - (Number(autoRuntime.pendingSendStartedAt) || origin);
    if (retryElapsed >= AUTO_SEND_REGISTER_RETRY_MS && !chatGPTIsGenerating() && receiptState === 'present-with-receipt') {
      retryPendingComposerSendClick(expectedKind).then(result => {
        if (result === 'retry-exhausted') {
          setStatus(`${waveLabel(expectedKind)} normal click-retry budget is exhausted. The same receipt remains under bounded observation; no duplicate command will be inserted.`, 'warning');
        }
        scheduleAutoAuditCheck(AUTO_LIVENESS_CHECK_MS);
      }).catch(error => {
        setStatus(`${waveLabel(expectedKind)} click-only Send recovery failed: ${error?.message || 'unexpected retry error'}. Pending receipt preserved.`, 'warning');
        scheduleAutoAuditCheck(AUTO_LIVENESS_CHECK_MS);
      });
      return;
    }

    setStatus(
      receiptState === 'composer-unavailable'
        ? `${waveLabel(expectedKind)} Send registration is temporarily unobservable because the composer is hydrating. This is not treated as success; the receipt remains under bounded recovery.`
        : `${waveLabel(expectedKind)} Send is awaiting receipt-bearing turn registration. Auto3 will not re-send without positive prepared-composer evidence.`,
      'info'
    );
    scheduleAutoAuditCheck(AUTO_LIVENESS_CHECK_MS);
  }

  function clearAutoComposerHold() {
    if (autoComposerHoldTimer) {
      clearTimeout(autoComposerHoldTimer);
      autoComposerHoldTimer = 0;
    }
    autoComposerHoldReason = '';
    autoComposerHoldSince = 0;
    autoComposerHoldAttempts = 0;
  }

  function composerReadinessIsSoftHold(reason = '') {
    return /still generating|composer is not available|manual draft|pending manual attachment|account\/authentication interstitial|logged-out\/account surface/i.test(String(reason || ''));
  }

  function autoComposerHoldKind() {
    if (!autoRuntime) return '';
    if (autoRuntime.stage === 'sending-second') return 'second';
    if (autoRuntime.stage === 'sending-performance') return 'performance';
    if (autoRuntime.stage === 'sending-continuation') return String(autoRuntime.continuationKind || '');
    return '';
  }

  function autoComposerHoldApplicable() {
    return Boolean(
      autoComposerHoldReason &&
      autoRuntime?.enabled &&
      ['sending-second', 'sending-performance', 'sending-continuation'].includes(autoRuntime.stage)
    );
  }

  function scheduleAutoComposerHoldProbe(kind = autoComposerHoldKind(), options = {}) {
    if (!autoComposerHoldApplicable()) return false;

    if (autoComposerHoldTimer) {
      if (!options.force) return true;
      clearTimeout(autoComposerHoldTimer);
      autoComposerHoldTimer = 0;
    }

    const index = Math.max(0, autoComposerHoldAttempts - 1);
    const fallbackDelay = index < AUTO_COMPOSER_HOLD_FAST_DELAYS_MS.length
      ? AUTO_COMPOSER_HOLD_FAST_DELAYS_MS[index]
      : AUTO_COMPOSER_HOLD_SAFETY_MS;
    const delay = Math.max(80, Number(options.delay ?? fallbackDelay));

    autoComposerHoldTimer = setTimeout(() => {
      autoComposerHoldTimer = 0;
      bindAutoRuntimeToCurrentConversation({ claim: false });

      if (!autoComposerHoldApplicable()) {
        clearAutoComposerHold();
        renderAutoAuditState();
        return;
      }

      const ready = chatGPTComposerReadyForAutoSend();

      if (ready.ok) {
        const recoveredKind = kind || autoComposerHoldKind();
        clearAutoComposerHold();
        setStatus(
          `${waveLabel(recoveredKind)} HOLD cleared automatically: the ChatGPT composer is ready again. Continuing Auto3 without toggling A3.`,
          'success'
        );
        renderAutoAuditState();
        scheduleAutoAuditCheck(0);
        return;
      }

      if (!composerReadinessIsSoftHold(ready.reason)) {
        clearAutoComposerHold();
        renderAutoAuditState();
        scheduleAutoAuditCheck(0);
        return;
      }

      const changed = autoComposerHoldReason !== ready.reason;
      autoComposerHoldReason = ready.reason;
      autoComposerHoldAttempts += 1;

      if (changed) {
        setStatus(
          `${waveLabel(kind || autoComposerHoldKind())} auto-send HOLD changed: ${ready.reason} Auto3 is still self-monitoring.`,
          'info'
        );
        renderAutoAuditState();
      }

      // Fast probes are bounded. Afterwards only one tiny 15s safety probe
      // remains while HOLD exists. Real composer DOM changes wake this sooner.
      scheduleAutoComposerHoldProbe(kind || autoComposerHoldKind());
    }, delay);

    return true;
  }

  function deferAutoSendForComposer(kind, reason) {
    const normalized = String(reason || 'Composer is not ready.').trim();

    if (autoComposerHoldReason !== normalized) {
      autoComposerHoldReason = normalized;
      autoComposerHoldSince = Date.now();
      autoComposerHoldAttempts = 0;
    }

    autoComposerHoldAttempts += 1;

    setStatus(
      `${waveLabel(kind)} auto-send HOLD: ${normalized} Auto3 will detect recovery itself; no A3 toggle or Resume is required.`,
      'info'
    );
    renderAutoAuditState();
    scheduleAutoComposerHoldProbe(kind);
    return false;
  }

  async function sendAutoAuditWave(kind) {
    const token = await verifyAutoLeaseForSend();
    if (!token) {
      refreshAutoRuntimeFromStorage();
      renderAutoAuditState();
      scheduleAutoAuditCheck(900);
      return false;
    }

    const mapping = {
      second: { name: 'Audit Second Wave', text: AUDIT_SECOND_WAVE, next: 'await-second-user' },
      performance: { name: 'Audit Performance', text: AUDIT_PERFORMANCE, next: 'await-performance-user' }
    };
    const wave = mapping[kind];
    if (!wave) return false;

    const ready = chatGPTComposerReadyForAutoSend();
    if (!ready.ok) {
      if (composerReadinessIsSoftHold(ready.reason)) {
        return deferAutoSendForComposer(kind, ready.reason);
      }
      pauseAutoAudit(ready.reason);
      return false;
    }

    clearAutoComposerHold();
    const ownership = createAutoSendOwnershipGuard(token, chatGPTComposerStateSnapshot());

    const previousUserId = kind === 'second'
      ? autoRuntime.coreUserId
      : autoRuntime.secondUserId;
    const receipt = ensurePendingSendReceipt(kind, previousUserId);
    if (!receipt || autoRuntime.stage === 'paused') return false;
    beginCommittedAutoSend(kind, receipt, { continuation: false, previousUserId });
    const preset = {
      name: wave.name,
      text: wave.text,
      machineReceipt: receipt
    };
    const result = await executePreset(preset, 'run', {
      quietBusy: true,
      autoOwnership: ownership,
      beforeSend: async () => {
        autoRuntime.stage = wave.next;
        autoRuntime.expectedKind = kind;
        autoRuntime.pendingSendClickArmed = true;
        autoRuntime.pendingSendStartedAt = Date.now();
        autoRuntime.waitStartedAt = Date.now();
        autoRuntime.stableResponseKey = '';
        autoRuntime.stableSince = 0;
        return saveAutoRuntime();
      }
    });

    if (result?.reason === 'ownership-lost') {
      refreshAutoRuntimeFromStorage();
      renderAutoAuditState();
      scheduleAutoAuditCheck(900);
      return false;
    }

    if (result?.reason === 'busy') {
      scheduleAutoAuditCheck(1200);
      return false;
    }

    if (!result?.sent) {
      clearCommittedAutoSend(receipt);
      setStatus(`Automatic ${wave.name} Send was not positively verified. Auto3 kept the receipt checkpoint and will adopt the turn if it appears or retry only the Send click.`, 'warning');
      scheduleAutoAuditCheck(900);
      return false;
    }

    markCommittedAutoSendClicked(kind, receipt, { continuation: false, previousUserId });

    if (!isLeaseTokenCurrent(token)) {
      setStatus(`${waveLabel(kind)} Send was clicked, then this tab lost the Auto3 lease. Receipt-based recovery will adopt the sent turn without duplication.`, 'info');
      renderAutoAuditState();
      scheduleAutoAuditCheck(900);
      return true;
    }

    autoRuntime.pendingSendStartedAt = Date.now();
    autoRuntime.pendingSendClickArmed = true;
    // beforeSend already persisted await-stage + receipt before the irreversible click.
    // Never add a post-click storage gate that can turn a successful Send into PAUSE.
    scheduleAutoAuditCheck(500);
    return true;
  }

  function preemptSameWaveContinuationFromLiveResult(kind, options = {}) {
    if (!autoRuntime || !kind) return false;

    const turns = options.turns || getChatGPTTurns();
    const previousId = String(
      options.previousUserId ||
      autoRuntime.continuationPreviousUserId ||
      waveUserId(kind) ||
      ''
    );

    let previous = previousId ? findTurnById(previousId, turns) : null;
    if (!previous) {
      const latestExpected = latestExpectedAuditUserTurn(kind, turns);
      const latestId = getTurnId(latestExpected);
      if (latestExpected && (!previousId || !latestId || latestId === previousId)) {
        previous = latestExpected;
      }
    }
    if (!previous) return false;

    const flow = auditUserFlowAfter(previous, kind, turns);

    if (flow.conflictingTurn) {
      pauseAutoAudit(
        `A different audit command (${waveLabel(flow.conflictingKind)}) appeared before the automatic ${waveLabel(kind)} continuation. Auto3 stopped to avoid crossing explicit audit lineages.`
      );
      return true;
    }

    // If the user/another automation instance already inserted the expected
    // same-wave continuation, adopt it instead of ever creating a duplicate.
    if (flow.expectedTurn) {
      const id = getTurnId(flow.expectedTurn);
      if (!id) {
        setStatus(
          `${waveLabel(kind)} continuation is already visible, but ChatGPT has not hydrated its stable turn id yet. Waiting; no duplicate continuation will be sent.`,
          'info'
        );
        scheduleAutoAuditCheck(700);
        return true;
      }

      setWaveUserId(kind, id);
      autoRuntime.seenUserId = id;
      autoRuntime.stage = waveWaitStage(kind);
      autoRuntime.expectedKind = '';
      autoRuntime.continuationKind = '';
      autoRuntime.continuationReason = '';
      autoRuntime.continuationPreviousUserId = '';
      autoRuntime.waitStartedAt = Date.now();
      autoRuntime.stableResponseKey = '';
      autoRuntime.stableSince = 0;
      autoRuntime.stageAssistantId = '';
      autoRuntime.anchorMissingSince = 0;
      autoRuntime.idleStallKey = '';
      autoRuntime.idleStallSince = 0;
      clearPendingSendReceipt({ save: false });
      clearAutoComposerHold();
      if (!saveAutoRuntime({ pauseOnFailure: false })) {
        scheduleAutoAuditCheck(900);
        return true;
      }
      setStatus(
        `${waveLabel(kind)} continuation already exists in the conversation. Auto3 adopted that exact turn and canceled the queued duplicate send.`,
        'success'
      );
      scheduleAutoAuditCheck(0);
      return true;
    }

    // Do not inspect a moving response as terminal. Once generation is idle,
    // however, a structurally COMPLETE/BLOCKED handoff outranks any previously
    // queued stall nudge. PARTIAL intentionally falls through and continues.
    if (chatGPTIsGenerating()) return false;

    const assistant = auditAssistantAcrossSupplementals(previous, kind, turns) ||
      assistantTurnAfter(previous, turns);
    if (!assistant) return false;

    const proof = responseGateFromAssistantTurn(waveWaitStage(kind), assistant);
    if (proof.state === 'blocked') {
      pauseAutoAudit(
        `The existing ${waveLabel(kind)} response reported BLOCKED before the queued continuation was sent. The stale continuation was canceled.`
      );
      return true;
    }

    if (proof.state !== 'complete') return false;

    // Re-enter the normal wait-stage consumer instead of duplicating advancement
    // logic here. evaluateAutoAudit() will stabilize, cache/save the exact handoff,
    // then advance Core -> Second -> Performance in canonical order.
    autoRuntime.stage = waveWaitStage(kind);
    autoRuntime.expectedKind = '';
    autoRuntime.continuationKind = '';
    autoRuntime.continuationReason = '';
    autoRuntime.continuationPreviousUserId = '';
    autoRuntime.waitStartedAt = Date.now();
    autoRuntime.stableResponseKey = '';
    autoRuntime.stableSince = 0;
    autoRuntime.stageAssistantId = getTurnId(assistant) || '';
    autoRuntime.anchorMissingSince = 0;
    autoRuntime.idleStallKey = '';
    autoRuntime.idleStallSince = 0;
    clearPendingSendReceipt({ save: false });
    clearCommittedAutoSend();
    clearAutoComposerHold();

    if (!saveAutoRuntime({ pauseOnFailure: false })) {
      setStatus(
        `${waveLabel(kind)} is already COMPLETE, but runtime persistence is temporarily unavailable. The stale continuation remains canceled and Auto3 will retry state reconciliation.`,
        'warning'
      );
      scheduleAutoAuditCheck(900);
      return true;
    }

    setStatus(
      `${waveLabel(kind)} became structurally COMPLETE before a queued liveness continuation could send. Stale CONTINUE canceled; consuming COMPLETE and advancing in canonical wave order.`,
      'success'
    );
    scheduleAutoAuditCheck(0);
    return true;
  }


  async function sendAutoAuditContinuation(kind) {
    if (!kind) return false;

    // Final irreversible-send preflight. A tab can freeze for minutes/hours after
    // queueing a stall nudge, then wake after the assistant has already produced
    // a COMPLETE handoff. Never trust stale `sending-continuation` state blindly.
    if (preemptSameWaveContinuationFromLiveResult(kind)) return true;

    const token = await verifyAutoLeaseForSend();
    if (!token) {
      refreshAutoRuntimeFromStorage();
      renderAutoAuditState();
      scheduleAutoAuditCheck(900);
      return false;
    }

    // Lease acquisition is asynchronous. Re-check once more immediately after it
    // so a terminal answer that landed during arbitration still preempts Send.
    if (preemptSameWaveContinuationFromLiveResult(kind)) return true;

    const reason = String(autoRuntime.continuationReason || 'partial');
    const count = Math.max(
      1,
      Number(
        reason === 'stall'
          ? autoRuntime.stallNudges?.[kind]
          : reason === 'sidecar'
            ? autoRuntime.sidecarRecoveries?.[kind]
            : autoRuntime.partialContinuations?.[kind]
      ) || 1
    );
    const ready = chatGPTComposerReadyForAutoSend();
    if (!ready.ok) {
      if (composerReadinessIsSoftHold(ready.reason)) {
        return deferAutoSendForComposer(kind, ready.reason);
      }
      pauseAutoAudit(ready.reason);
      return false;
    }

    clearAutoComposerHold();
    const ownership = createAutoSendOwnershipGuard(token, chatGPTComposerStateSnapshot());

    const receipt = ensurePendingSendReceipt(
      kind,
      autoRuntime.continuationPreviousUserId || waveUserId(kind)
    );
    if (!receipt || autoRuntime.stage === 'paused') return false;
    beginCommittedAutoSend(kind, receipt, { continuation: true, previousUserId: autoRuntime.continuationPreviousUserId || waveUserId(kind) });
    const preset = {
      name: `${waveLabel(kind)} Continue ${count}`,
      text: auditContinuationPrompt(kind, count, reason),
      forceTextDelivery: true,
      machineReceipt: receipt
    };
    const result = await executePreset(preset, 'run', {
      quietBusy: true,
      autoOwnership: ownership,
      beforeSend: async () => {
        autoRuntime.stage = 'await-continuation-user';
        autoRuntime.expectedKind = kind;
        autoRuntime.pendingSendClickArmed = true;
        autoRuntime.pendingSendStartedAt = Date.now();
        autoRuntime.waitStartedAt = Date.now();
        autoRuntime.stableResponseKey = '';
        autoRuntime.stableSince = 0;
        return saveAutoRuntime();
      }
    });

    if (result?.reason === 'ownership-lost') {
      refreshAutoRuntimeFromStorage();
      renderAutoAuditState();
      scheduleAutoAuditCheck(900);
      return false;
    }

    if (result?.reason === 'busy') {
      scheduleAutoAuditCheck(1200);
      return false;
    }

    if (!result?.sent) {
      clearCommittedAutoSend(receipt);
      setStatus(`${waveLabel(kind)} continuation Send was not positively verified. Auto3 kept the receipt checkpoint and will adopt the turn if it appears or retry only the Send click.`, 'warning');
      scheduleAutoAuditCheck(900);
      return false;
    }

    markCommittedAutoSendClicked(kind, receipt, { continuation: true, previousUserId: autoRuntime.continuationPreviousUserId || waveUserId(kind) });

    if (!isLeaseTokenCurrent(token)) {
      setStatus(`${waveLabel(kind)} continuation was clicked before this tab lost the lease. Receipt recovery prevents false PAUSE and duplicate continuation.`, 'info');
      renderAutoAuditState();
      scheduleAutoAuditCheck(900);
      return true;
    }

    autoRuntime.pendingSendStartedAt = Date.now();
    autoRuntime.pendingSendClickArmed = true;
    scheduleAutoAuditCheck(500);
    return true;
  }

  function scheduleAuditContinuation(kind) {
    if (autoAuditNextTimer) return;
    const previousId = autoRuntime.continuationPreviousUserId || waveUserId(kind);
    const delay = Math.max(500, Number(state.autoAuditDelayMs) || 1200);

    autoAuditNextTimer = setTimeout(async () => {
      autoAuditNextTimer = 0;
      if (!autoRuntime?.enabled || autoRuntime.stage !== 'sending-continuation' || autoRuntime.continuationKind !== kind) return;

      const turns = getChatGPTTurns();

      // A queued continuation is only an intention, never authority. Before Send,
      // re-read the exact preceding audit turn. An existing same-wave command,
      // BLOCKED, or structurally COMPLETE response always wins over the stale nudge.
      if (preemptSameWaveContinuationFromLiveResult(kind, {
        previousUserId: previousId,
        turns
      })) return;

      // Plain user messages/files remain supplemental context. If no stronger
      // terminal/explicit evidence exists, send the expected continuation now.
      await sendAutoAuditContinuation(kind);
    }, delay);
  }

  function scheduleNextWave(kind) {
    if (autoAuditNextTimer) return;
    const delay = state.autoAuditDelayMs;
    const stageExpected = kind === 'second' ? 'sending-second' : 'sending-performance';

    autoAuditNextTimer = setTimeout(async () => {
      autoAuditNextTimer = 0;
      if (!autoRuntime?.enabled || autoRuntime.stage !== stageExpected) return;

      const turns = getChatGPTTurns();
      const anchorId = kind === 'second' ? autoRuntime.coreUserId : autoRuntime.secondUserId;
      const anchor = findTurnById(anchorId, turns);
      const flow = anchor ? auditUserFlowAfter(anchor, kind, turns) : null;

      if (flow?.conflictingTurn) {
        pauseAutoAudit(`A different audit command (${waveLabel(flow.conflictingKind)}) appeared before ${waveLabel(kind)}. Auto3 stopped only because an explicit competing audit lineage exists.`);
        return;
      }

      if (flow?.expectedTurn) {
        // The user manually sent the exact expected next wave. Adopt it instead
        // of duplicating it; any plain messages around it remain supplemental.
        const id = getTurnId(flow.expectedTurn);
        autoRuntime.seenUserId = id;
        autoRuntime.waitStartedAt = Date.now();
        autoRuntime.stableResponseKey = '';
        autoRuntime.stableSince = 0;
        autoRuntime.stageAssistantId = '';
        autoRuntime.anchorMissingSince = 0;
        if (kind === 'second') {
          autoRuntime.secondUserId = id;
          autoRuntime.stage = 'wait-second';
        } else {
          autoRuntime.performanceUserId = id;
          autoRuntime.stage = 'wait-performance';
        }
        if (!saveAutoRuntime()) return;
        scheduleAutoAuditCheck(0);
        return;
      }

      await sendAutoAuditWave(kind);
    }, delay);
  }

  function reconcileEnabledIdleAuditRuntime(turns = getChatGPTTurns(), options = {}) {
    if (!autoRuntime?.enabled || autoRuntime.stage !== 'idle') return false;
    if (chatGPTAuthInterstitialVisible()) return false;

    // Rebuild the live lineage BEFORE backfilling COMPLETE records. Backfilling
    // first can create a provisional runId while coreUserId is still empty; the
    // subsequent rebuild then correctly decides it is a new lineage, creates a
    // second runId, and deletes the just-captured record. On a virtualized DOM
    // that needless churn can lose the only COMPLETE proof. Establish the run
    // identity first, then capture under that exact run.
    const liveLineage = visibleAuditLineage(turns);
    if (liveLineage.blockedByReset) return false;
    const latestAudit = liveLineage.performance || liveLineage.second || liveLineage.core;
    if (latestAudit) {
      const id = getTurnId(latestAudit);
      const kind = classifyAuditTurn(latestAudit);

      if (!id) {
        setStatus(
          `Auto3 found the latest ${waveLabel(kind)} command but ChatGPT has not hydrated its stable turn id yet. Recovery will retry automatically.`,
          'info'
        );
        scheduleAutoAuditCheck(700);
        return true;
      }

      if (resumeRuntimeFromAuditTurn(latestAudit, { turns })) {
        backfillVisibleCompletedAuditResults();
        setStatus(
          `Auto3 reconciled enabled+READY state from the live ${waveLabel(kind)} lineage. Continuing automatically; no OFF/ON toggle or Resume is required.`,
          'success'
        );
        return true;
      }
    }

    // DOM virtualization fallback. Audit result records are scoped to this exact
    // conversation and are written only from structurally COMPLETE handoffs.
    const coherentRecords = currentChatAuditRecords();
    const core = coherentRecords.find(record => record.kind === 'core') || null;
    const second = coherentRecords.find(record => record.kind === 'second') || null;
    const performance = coherentRecords.find(record => record.kind === 'performance') || null;
    const latestRecord = performance || second || core;
    if (!latestRecord?.text) return false;

    let nextStage = '';
    if (performance?.text) nextStage = 'complete';
    else if (second?.text) nextStage = 'sending-performance';
    else if (core?.text) nextStage = 'sending-second';
    if (!nextStage) return false;

    autoRuntime.stage = nextStage;
    autoRuntime.runId = String(latestRecord.runId || autoRuntime.runId || createAuditRunId());
    autoRuntime.startedAt = Number(autoRuntime.startedAt || latestRecord.completedAt || Date.now());
    autoRuntime.waitStartedAt = nextStage === 'complete' ? 0 : Date.now();
    autoRuntime.projectName = sanitizeProjectIdentity(
      latestRecord.projectName || autoRuntime.projectName || ''
    );
    autoRuntime.projectNameSource = autoRuntime.projectName
      ? (autoRuntime.projectNameSource || 'handoff')
      : '';
    autoRuntime.pausedReason = '';
    autoRuntime.pausedFromStage = '';
    autoRuntime.expectedKind = '';
    autoRuntime.stableResponseKey = '';
    autoRuntime.stableSince = 0;
    autoRuntime.anchorMissingSince = 0;
    clearAutoComposerHold();

    if (nextStage === 'complete') {
      autoRuntime.completeAt = autoRuntime.completeAt || Number(performance?.completedAt) || Date.now();
    }

    if (!saveAutoRuntime({ pauseOnFailure: false })) {
      scheduleAutoAuditCheck(1000);
      return true;
    }

    if (nextStage === 'complete') {
      setStatus(
        'Auto3 reconciled READY from cached COMPLETE Core + Second + Performance handoffs. Chain is DONE.',
        'success'
      );
      return true;
    }

    const nextKind = nextStage === 'sending-second' ? 'second' : 'performance';
    setStatus(
      `Auto3 reconciled READY from cached COMPLETE audit evidence. Continuing with ${waveLabel(nextKind)} automatically.`,
      'success'
    );
    scheduleAutoAuditCheck(0);
    return true;
  }

  async function evaluateAutoAudit(options = {}) {
    if (autoAuditEvaluating) return;
    if (detectSite().key !== 'chatgpt') return;

    autoAuditEvaluating = true;
    try {
      if (
        autoComposerHoldReason &&
        !['sending-second', 'sending-performance', 'sending-continuation'].includes(autoRuntime?.stage)
      ) {
        clearAutoComposerHold();
        renderAutoAuditState();
      }

      const turns = getChatGPTTurns();

      if (chatGPTAuthInterstitialVisible() || chatGPTRootIsQuarantined()) {
        setStatus(
          'AUTH HOLD: ChatGPT is showing a logged-out/account surface. AICHATBUTTONS is quarantined: no send, rename, navigation, lease, or audit-state mutation runs on this root page.',
          'warning'
        );
        renderAutoAuditState();
        scheduleAutoAuditCheck(AUTO_AUTH_HOLD_RETRY_MS);
        return;
      }

      const startHandoff = readStartAuditHandoff();
      if (startHandoffIsCommitted(startHandoff) && !autoRuntime?.enabled) {
        recoverSentStartCore({
          source: 'evaluate-bootstrap',
          skipMonitor: true
        });
      }

      if (startHandoffIsPrepared(startHandoff)) {
        const visibleReceipt = startHandoff.receipt && exactReceiptUserTurn(startHandoff.receipt, turns);
        if (visibleReceipt) {
          markStartAuditHandoffSent(startHandoff);
          recoverSentStartCore({ source: 'evaluate-visible-start-receipt', skipMonitor: true });
          scheduleSentStartRecovery();
        } else if (startHandoffComposerStillPrepared(startHandoff)) {
          const recoveredClick = await recoverArmedStartSend({ waitMs: 900, reschedule: true });
          if (recoveredClick) return;
        }
      }

      if (!ensureAutoConversation(turns)) return;

      // Do this only after lease ownership/reload reconciliation. Standby tabs
      // must never rewrite shared runtime clocks merely because their own timer
      // was throttled while another tab owned the conversation.
      reconcileExecutionGap();

      const latestUser = latestChatGPTUserTurn(turns);
      const latestUserId = getTurnId(latestUser);
      const latestKind = classifyAuditTurn(latestUser);

      if (autoRuntime.stage === 'complete') {
        if (latestKind === 'core') {
          if (!latestUserId) {
            setStatus('A new AUDIT CORE is visible. Waiting for ChatGPT to hydrate its stable turn identity before re-arming Auto3.', 'info');
            scheduleAutoAuditCheck(800);
            return;
          }
          if (latestUserId !== autoRuntime.seenUserId) {
            resetAutoAuditRuntime({ silent: true });
            // The tab may have been frozen/offscreen until this new Core already
            // finished. A fresh authored Core must still be adopted; completion
            // is consumed by the normal wait-core gate on the next evaluation.
            armFromCoreTurn(latestUser, { allowCompleted: true });
          }
        }
        return;
      }

      if (autoRuntime.stage === 'idle') {
        if (reconcileEnabledIdleAuditRuntime(turns, { source: 'evaluate-idle' })) return;

        if (!autoRuntime.seenUserId) {
          autoRuntime.seenUserId = latestUserId;
          autoRuntime.baselineAssistantKey = assistantFingerprint(assistantTurnAfter(latestUser, turns));
          if (!saveAutoRuntime()) return;
        }

        const isFreshUser = latestUserId && latestUserId !== autoRuntime.seenUserId;
        if (isFreshUser) {
          autoRuntime.seenUserId = latestUserId;
          autoRuntime.baselineAssistantKey = assistantFingerprint(assistantTurnAfter(latestUser, turns));
          if (!saveAutoRuntime()) return;
          if (latestKind === 'core') {
            resetAutoAuditRuntime({ silent: true });
            armFromCoreTurn(latestUser, { allowCompleted: false });
            return;
          }
        }

        if (options.adoptCurrent) {
          adoptCurrentAuditTurn();
          return;
        }

        const currentAssistantKey = assistantFingerprint(assistantTurnAfter(latestUser, turns));
        const coreActivityChanged = latestKind === 'core' &&
          currentAssistantKey &&
          currentAssistantKey !== autoRuntime.baselineAssistantKey;

        // This catches the common case where Auto was enabled after Core was sent
        // but while ChatGPT was already thinking/streaming. Old completed Core turns
        // remain inert because their fingerprint does not change.
        if (latestKind === 'core' && (chatGPTIsGenerating() || coreActivityChanged)) {
          resetAutoAuditRuntime({ silent: true });
          armFromCoreTurn(latestUser, { allowCompleted: false });
          return;
        }

        return;
      }

      if (autoRuntime.stage === 'paused') {
        if (recoverCommittedSendFromDom(turns)) return;
        if (recoverStalePauseFromConversation(turns, { source: 'evaluate' })) return;
        return;
      }

      if (['await-second-user', 'await-performance-user', 'await-continuation-user'].includes(autoRuntime.stage) && recoverCommittedSendFromDom(turns)) return;

      if (autoRuntime.stage === 'sending-continuation') {
        scheduleAuditContinuation(autoRuntime.continuationKind);
        return;
      }

      if (autoRuntime.stage === 'sending-second') {
        scheduleNextWave('second');
        return;
      }

      if (autoRuntime.stage === 'sending-performance') {
        scheduleNextWave('performance');
        return;
      }

      if (autoRuntime.stage === 'await-continuation-user') {
        const expectedKind = autoRuntime.expectedKind || autoRuntime.pendingSendKind || autoRuntime.continuationKind;

        if (recoverPendingSendRegistration(expectedKind, turns)) return;

        const elapsed = Date.now() - (
          Number(autoRuntime.pendingSendStartedAt) ||
          Number(autoRuntime.waitStartedAt) ||
          Date.now()
        );

        if (elapsed > AUTO_SEND_REGISTER_TIMEOUT_MS) {
          scheduleRegistrationRecovery(expectedKind, 'sending-continuation');
          return;
        }

        scheduleAutoAuditCheck(800);
        return;
      }

      if (autoRuntime.stage === 'await-second-user' || autoRuntime.stage === 'await-performance-user') {
        const expectedKind = autoRuntime.expectedKind;

        if (recoverPendingSendRegistration(expectedKind, turns)) return;

        const elapsed = Date.now() - (
          Number(autoRuntime.pendingSendStartedAt) ||
          Number(autoRuntime.waitStartedAt) ||
          Date.now()
        );

        if (elapsed > AUTO_SEND_REGISTER_TIMEOUT_MS) {
          scheduleRegistrationRecovery(
            expectedKind,
            expectedKind === 'second' ? 'sending-second' : 'sending-performance'
          );
          return;
        }

        scheduleAutoAuditCheck(800);
        return;
      }

      const currentWaveKind = activeWaveKind(autoRuntime.stage);
      const savedStageAnchorId = activeStageAnchorId(autoRuntime.stage);
      let anchor = findTurnById(savedStageAnchorId, turns);
      if (!anchor) {
        anchor = recoverExpectedStageAnchor(autoRuntime.stage, turns);
      }

      let assistant = null;

      if (anchor) {
        autoRuntime.anchorMissingSince = 0;

        const flow = auditUserFlowAfter(anchor, currentWaveKind, turns);

        if (flow.conflictingTurn) {
          pauseAutoAudit(`A different explicit audit command (${waveLabel(flow.conflictingKind)}) appeared while ${waveLabel(currentWaveKind)} was active. Auto3 stopped to avoid crossing audit lineages.`);
          return;
        }

        if (flow.expectedTurn && getTurnId(flow.expectedTurn) !== getTurnId(anchor)) {
          // A manual same-wave continuation was inserted. Rebuild around that
          // explicit audit command; supplemental messages do not invalidate it.
          if (resumeRuntimeFromAuditTurn(flow.expectedTurn, { turns })) return;
        }

        assistant = auditAssistantAcrossSupplementals(anchor, currentWaveKind, turns);
        if (assistant) {
          rememberStageAssistant(assistant);
        } else if (flow.supplementals.length) {
          await watchSupplementalAuditContext(currentWaveKind, anchor, flow, turns);
          return;
        }
      } else {
        // Missing from the mounted DOM is not missing from the conversation.
        // Long ChatGPT responses routinely virtualize the user turn above them.
        if (!autoRuntime.anchorMissingSince) {
          autoRuntime.anchorMissingSince = Date.now();
          saveAutoRuntime({ pauseOnFailure: false });
        }

        if (visibleUserConflictsWithActiveStage(turns)) {
          pauseAutoAudit('A different explicit audit command is visible while the saved audit anchor is virtualized. Auto3 stopped to avoid crossing audit lineages.');
          return;
        }

        assistant = recoverVirtualizedStageAssistant(turns);

        if (!assistant) {
          setStatus(
            `${waveLabel(currentWaveKind)} anchor is temporarily outside ChatGPT's mounted DOM. Auto3 preserved lineage and is watching for the active response.`,
            'info'
          );

          if (chatGPTIsGenerating()) {
            resetIdleStallWatch();
            scheduleAutoAuditCheck(AUTO_LIVENESS_CHECK_MS);
            return;
          }

          await watchIdleAuditStall(currentWaveKind, null, 'anchor-virtualized');
          return;
        }

        setStatus(
          `${waveLabel(currentWaveKind)} user anchor is virtualized, but the active assistant response is still mounted. Auto3 recovered it directly.`,
          'success'
        );
      }

      if (assistant) rememberStageAssistant(assistant);

      if (!assistant) {
        if (chatGPTIsGenerating()) {
          resetIdleStallWatch();
          scheduleAutoAuditCheck(AUTO_LIVENESS_CHECK_MS);
          return;
        }

        await watchIdleAuditStall(currentWaveKind, null, 'no-assistant');
        if (stageTimedOut() && autoRuntime.stage.startsWith('wait-')) {
          pauseAutoAudit(`No assistant response appeared within ${state.autoAuditTimeoutMin} minutes even after unattended liveness recovery attempts.`);
        }
        return;
      }

      if (chatGPTIsGenerating()) {
        resetIdleStallWatch();
        scheduleAutoAuditCheck(AUTO_LIVENESS_CHECK_MS);
        return;
      }

      if (assistantNeedsContinuation(assistant)) {
        resetIdleStallWatch();
        const handled = await autoClickAssistantRecovery(assistant, 'continue', currentWaveKind);
        if (!handled && autoRuntime.stage !== 'paused') scheduleAutoAuditCheck(1000);
        return;
      }

      if (assistantHasRetryError(assistant)) {
        resetIdleStallWatch();
        const handled = await autoClickAssistantRecovery(assistant, 'retry', currentWaveKind);
        if (!handled && autoRuntime.stage !== 'paused') scheduleAutoAuditCheck(1500);
        return;
      }

      const final = completedAssistantCandidate(assistant, autoRuntime.stage);
      if (!final.complete) {
        if (final.reason === 'stabilizing') return;

        await watchIdleAuditStall(currentWaveKind, assistant, final.reason || 'incomplete');
        if (stageTimedOut() && autoRuntime.stage.startsWith('wait-')) {
          pauseAutoAudit(`No verified final response within ${state.autoAuditTimeoutMin} minutes even after unattended liveness recovery attempts.`);
        }
        return;
      }

      const gate = final.gate && final.gate !== 'unknown'
        ? final.gate
        : responseGateFromAssistantTurn(autoRuntime.stage, assistant).state;
      if (gate === 'blocked') {
        resetIdleStallWatch();
        pauseAutoAudit(`The ${waveLabel(activeWaveKind(autoRuntime.stage))} response reported BLOCKED. This is treated as a hard audit precondition failure rather than a normal unfinished wave.`);
        return;
      }

      if (gate === 'partial') {
        resetIdleStallWatch();
        queueSameWaveContinuation(activeWaveKind(autoRuntime.stage), 'partial');
        return;
      }

      if (gate === 'unknown' && state.autoAuditStrictGate) {
        await watchIdleAuditStall(currentWaveKind, assistant, 'terminal-status-missing');
        return;
      }

      // Persist the final handoff before changing stage. DOM virtualization, reload,
      // later continuations and sidebar churn can no longer make a completed audit
      // disappear from one-click copy or optional disk output.
      const capturedFinal = captureCompletedAudit(
        currentWaveKind,
        final.text,
        gate,
        waveUserId(currentWaveKind)
      );
      if (!capturedFinal) {
        // The terminal handoff is visible but its durable cache commit failed or
        // was fenced by a newer/reset lineage. Never advance to the next wave
        // without the exact COMPLETE evidence that continuation/reload/save logic
        // depends on. Stay on this wave and retry/reconcile instead.
        setStatus(
          `${waveLabel(currentWaveKind)} is COMPLETE in the conversation, but its durable handoff commit was not accepted. Auto3 is holding this wave and will retry/reconcile without sending the next wave.`,
          'warning'
        );
        scheduleAutoAuditCheck(1200);
        return;
      }

      if (autoRuntime.stage === 'wait-core') {
        autoRuntime.continuationKind = '';
        autoRuntime.continuationReason = '';
        autoRuntime.continuationPreviousUserId = '';
        clearStageAssistant({ save: false });
        resetIdleStallWatch({ save: false });
        autoRuntime.stage = 'sending-second';
        autoRuntime.waitStartedAt = Date.now();
        if (!saveAutoRuntime()) return;
        scheduleNextWave('second');
        return;
      }

      if (autoRuntime.stage === 'wait-second') {
        autoRuntime.continuationKind = '';
        autoRuntime.continuationReason = '';
        autoRuntime.continuationPreviousUserId = '';
        clearStageAssistant({ save: false });
        resetIdleStallWatch({ save: false });
        autoRuntime.stage = 'sending-performance';
        autoRuntime.waitStartedAt = Date.now();
        if (!saveAutoRuntime()) return;
        scheduleNextWave('performance');
        return;
      }

      if (autoRuntime.stage === 'wait-performance') {
        clearAutoComposerHold();
        autoRuntime.continuationKind = '';
        autoRuntime.continuationReason = '';
        autoRuntime.continuationPreviousUserId = '';
        clearStageAssistant({ save: false });
        resetIdleStallWatch({ save: false });
        autoRuntime.stage = 'complete';
        autoRuntime.completeAt = Date.now();
        autoRuntime.waitStartedAt = 0;
        autoRuntime.stableResponseKey = '';
        autoRuntime.stableSince = 0;
        if (!saveAutoRuntime()) return;
        setStatus('Auto audit chain complete: Core -> Second Wave -> Performance all received final responses.', 'success');
      }
    } finally {
      autoAuditEvaluating = false;
    }
  }

  function scheduleAutoAuditCheck(delay = AUTO_OBSERVER_DEBOUNCE_MS) {
    if (detectSite().key !== 'chatgpt') return;
    bindAutoRuntimeToCurrentConversation({ claim: false });
    if (!autoRuntime?.enabled) return;
    if (autoAuditCheckTimer) clearTimeout(autoAuditCheckTimer);
    autoAuditCheckTimer = setTimeout(() => {
      autoAuditCheckTimer = 0;
      evaluateAutoAudit().catch(error => {
        pauseAutoAudit(`Monitor error: ${error?.message || 'unexpected runtime error'}.`);
      });
    }, Math.max(0, delay));
  }

  function stageForAuditKind(kind) {
    if (kind === 'core') return 'wait-core';
    if (kind === 'second') return 'wait-second';
    if (kind === 'performance') return 'wait-performance';
    return '';
  }

  function auditKindForStage(stage) {
    if (stage === 'wait-core') return 'core';
    if (stage === 'wait-second') return 'second';
    if (stage === 'wait-performance') return 'performance';
    return '';
  }

  function latestRecognizableAuditUserTurn(turns = getChatGPTTurns()) {
    for (let index = turns.length - 1; index >= 0; index -= 1) {
      const turn = turns[index];
      if (turnRole(turn) !== 'user') continue;
      if (classifyAuditTurn(turn)) return turn;
    }
    return null;
  }

  function resumeRuntimeFromAuditTurn(userTurn, options = {}) {
    if (!userTurn || !autoRuntime) return false;
    const turns = options.turns || getChatGPTTurns();
    const kind = classifyAuditTurn(userTurn);
    const stage = stageForAuditKind(kind);
    const id = getTurnId(userTurn);
    if (!kind || !stage || !id) return false;

    // Rebuild only from the coherent newest audit lineage. The old implementation
    // searched backward by type, so Core A -> Second A -> Core B -> Performance
    // could incorrectly pair Performance with Second A/Core A and jump across the
    // explicit Core B boundary.
    const lineage = visibleAuditLineage(turns);
    if (lineage.blockedByReset || lineage[kind] !== userTurn) return false;

    let coreUserId = '';
    let secondUserId = '';
    let performanceUserId = '';

    if (kind === 'core') {
      coreUserId = id;
    } else if (kind === 'second') {
      coreUserId = getTurnId(lineage.core);
      if (!lineage.core || !coreUserId) return false;
      secondUserId = id;
    } else {
      coreUserId = getTurnId(lineage.core);
      secondUserId = getTurnId(lineage.second);
      if (!lineage.core || !lineage.second || !coreUserId || !secondUserId) return false;
      performanceUserId = id;
    }

    // Validation above is side-effect-free. Commit one complete rebuilt runtime only
    // after the whole lineage is known to be valid. Same-wave CONTINUE commands are
    // new user-turn anchors but not new audit runs, so preserve run identity when
    // the persisted runtime was already working that same wave.
    const runtimeWaveKind = activeWaveKind(autoRuntime.stage) ||
      auditKindForStage(autoRuntime.pausedFromStage) ||
      String(autoRuntime.continuationKind || '');
    const continuingCurrentRun = Boolean(
      autoRuntime.runId &&
      auditTurnIsContinuation(userTurn) &&
      runtimeWaveKind === kind
    );
    const sameRuntimeLineage = Boolean(
      autoRuntime.runId &&
      (
        (autoRuntime.coreUserId && String(autoRuntime.coreUserId) === String(coreUserId)) ||
        continuingCurrentRun
      )
    );

    const inferredProjectName = sanitizeProjectIdentity(projectNameFromCoreTurn(lineage.core));
    const previousProjectName = sanitizeProjectIdentity(autoRuntime.projectName || '');
    const lineageProjectName = sameRuntimeLineage
      ? (inferredProjectName || previousProjectName)
      : inferredProjectName;
    const projectChanged = lineageProjectName !== previousProjectName;

    const nextRuntime = {
      ...autoRuntime,
      conversationKey: currentConversationKey(),
      anchorUserId: sameRuntimeLineage ? (autoRuntime.anchorUserId || coreUserId || id) : (coreUserId || id),
      seenUserId: id,
      runId: sameRuntimeLineage ? autoRuntime.runId : createAuditRunId(),
      startedAt: sameRuntimeLineage ? (autoRuntime.startedAt || Date.now()) : Date.now(),
      waitStartedAt: Date.now(),
      stableResponseKey: '',
      stableSince: 0,
      pausedReason: '',
      pausedFromStage: '',
      expectedKind: '',
      pendingSendReceipt: '',
      pendingSendKind: '',
      pendingSendPreviousUserId: '',
      pendingSendStartedAt: 0,
      pendingSendRetries: 0,
      pendingSendClickArmed: false,
      stageAssistantId: '',
      resetBarrierActive: false,
      resetBarrierUserId: '',
      anchorMissingSince: 0,
      projectName: lineageProjectName,
      projectNameSource: lineageProjectName ? (inferredProjectName ? 'artifact' : autoRuntime.projectNameSource || 'artifact') : '',
      renameAppliedName: (!sameRuntimeLineage || projectChanged) ? '' : autoRuntime.renameAppliedName,
      renamePersistedName: (!sameRuntimeLineage || projectChanged) ? '' : autoRuntime.renamePersistedName,
      renamePersistedAt: (!sameRuntimeLineage || projectChanged) ? 0 : autoRuntime.renamePersistedAt,
      renameAttemptName: (!sameRuntimeLineage || projectChanged) ? '' : autoRuntime.renameAttemptName,
      renameAttemptCount: (!sameRuntimeLineage || projectChanged) ? 0 : autoRuntime.renameAttemptCount,
      stage,
      coreUserId,
      secondUserId,
      performanceUserId
    };

    clearAutoTimers();
    autoRuntime = nextRuntime;
    if (!saveAutoRuntime()) return false;

    // Re-anchor Auto3 intent to the now-committed conversation key. After a
    // manual ChatGPT Send (or a draft -> c: route change) the original START
    // intent still carried the pre-send key, so a3IntentAllowsConversation
    // failed and the A3 checkbox dropped, forcing a manual re-enable. Keep it
    // engaged whenever an audit runtime is live and reconciled for this chat.
    writeA3Intent(true, currentConversationKey(), { startTransaction: true });

    if (!sameRuntimeLineage) {
      // A newly reconstructed Core lineage must not inherit one-record-per-wave
      // cache entries from an older run in the same conversation.
      clearAuditResultsForConversation(currentConversationKey());
    }

    if (autoRuntime.projectName) {
      const renameContext = {
        source: autoRuntime.projectNameSource || 'artifact',
        conversationKey: currentConversationKey(),
        runStartedAt: autoRuntime.startedAt || 0
      };
      maybeRenameConversation(autoRuntime.projectName, renameContext).catch(() => { });
      scheduleConversationTitleGuard(autoRuntime.projectName, renameContext);
    }

    const assistant = assistantTurnAfter(userTurn, turns);
    const proof = assistant ? responseGateFromAssistantTurn(stage, assistant) : { state: 'unknown', sourceCount: 0 };

    if (proof.state === 'complete') {
      setStatus(`Resume rebuilt the ${kind === 'core' ? 'Core' : kind === 'second' ? 'Second Wave' : 'Performance'} lineage atomically and found a COMPLETE handoff across ${proof.sourceCount || 1} response surface(s).`, 'success');
    } else {
      setStatus(`Resume rebuilt the current ${kind === 'core' ? 'Core' : kind === 'second' ? 'Second Wave' : 'Performance'} lineage atomically. Waiting for verifiable completion.`, 'success');
    }

    scheduleAutoAuditCheck(0);
    return true;
  }

  function resumeAutoAuditFromConversation() {
    bindAutoRuntimeToCurrentConversation({ claim: false });
    if (!autoRuntime?.enabled) {
      setStatus('Enable Auto 3 waves for this chat before Resume.', 'warning');
      return false;
    }
    if (!claimAutoLease()) {
      setStatus('Resume is in standby because another tab currently controls this same ChatGPT conversation.', 'warning');
      return false;
    }

    let turns = getChatGPTTurns();

    if (
      autoRuntime.stage === 'paused' &&
      recoverStalePauseFromConversation(turns, { source: 'manual-resume' })
    ) {
      return true;
    }

    let latestUser = latestChatGPTUserTurn(turns);
    let lineage = visibleAuditLineage(turns);
    let latestAudit = lineage.blockedByReset
      ? null
      : (lineage.performance || lineage.second || lineage.core);

    if (!latestAudit) {
      // Emergency semantic fallback. Only authored ChatGPT message nodes are
      // eligible, so visible UI/help text can never become an audit command.
      const fallbackTurns = [];
      const seen = new Set();

      for (const message of document.querySelectorAll(
        '[data-message-author-role="user"], [data-message-author-role="assistant"]'
      )) {
        const role = String(message.getAttribute('data-message-author-role') || '').toLowerCase();
        if (role !== 'user' && role !== 'assistant') continue;
        const wrapper = message.closest?.(
          'section[data-turn], article[data-turn], ' +
          'section[data-testid^="conversation-turn-"], article[data-testid^="conversation-turn-"], ' +
          '[data-testid^="conversation-turn-"]'
        ) || message;
        if (seen.has(wrapper)) continue;
        seen.add(wrapper);
        fallbackTurns.push(wrapper);
      }

      if (fallbackTurns.length) {
        fallbackTurns.sort((a, b) => {
          if (a === b) return 0;
          const relation = a.compareDocumentPosition(b);
          if (relation & Node.DOCUMENT_POSITION_FOLLOWING) return -1;
          if (relation & Node.DOCUMENT_POSITION_PRECEDING) return 1;
          return 0;
        });
        turns = fallbackTurns;
        latestUser = latestChatGPTUserTurn(turns);
        lineage = visibleAuditLineage(turns);
        latestAudit = lineage.blockedByReset
          ? null
          : (lineage.performance || lineage.second || lineage.core);
      }
    }

    // Plain user messages/files after an audit command are supplemental context.
    // Only a newer canonical audit command can replace the current audit lineage,
    // and latestAudit already points at that newest explicit audit command.
    if (!latestAudit) {
      const userCount = turns.filter(turn => turnRole(turn) === 'user').length;
      setStatus(`Resume failed: ${userCount} user turn(s) are visible, but none match canonical AUDIT CORE / SECOND WAVE / PERFORMANCE framing.`, 'warning');
      return false;
    }

    return resumeRuntimeFromAuditTurn(latestAudit, { turns });
  }

  function recoverStalePauseFromConversation(turns = getChatGPTTurns(), options = {}) {
    if (!autoRuntime?.enabled || autoRuntime.stage !== 'paused') return false;

    const reason = String(autoRuntime.pausedReason || '');
    if (pauseIsExplicitHumanStop(reason)) return false;

    // Strongest evidence first: exact machine receipt / committed click.
    if (recoverCommittedSendFromDom(turns)) return true;

    const latestUser = latestChatGPTUserTurn(turns);
    const liveLineage = visibleAuditLineage(turns);
    const latestAudit = liveLineage.blockedByReset
      ? null
      : (liveLineage.performance || liveLineage.second || liveLineage.core);

    // A newer plain user turn is supplemental context, not an audit-lineage
    // breaker. The newest canonical audit command remains the lineage anchor.
    if (latestAudit) {
      const kind = classifyAuditTurn(latestAudit);
      const stage = stageForAuditKind(kind);
      const id = getTurnId(latestAudit);

      if (kind && stage && id) {
        const rebuilt = resumeRuntimeFromAuditTurn(latestAudit, { turns });
        if (rebuilt) {
          clearAutoComposerHold();
          setStatus(
            `Auto3 self-healed a stale PAUSE from the live ${waveLabel(kind)} lineage. No manual Resume was required.`,
            'success'
          );
          renderAutoAuditState();
          return true;
        }
      }
    }

    // DOM virtualization / reload fallback. COMPLETE handoffs were already
    // cached by conversation before stage advancement, so they can prove how
    // far this exact audit run had safely reached.
    const coherentRecords = currentChatAuditRecords();
    const core = coherentRecords.find(record => record.kind === 'core') || null;
    const second = coherentRecords.find(record => record.kind === 'second') || null;
    const performance = coherentRecords.find(record => record.kind === 'performance') || null;
    const currentRunId = String(autoRuntime.runId || '');

    const belongsToCurrentRun = record => Boolean(
      record?.text &&
      (!currentRunId || (record.runId && String(record.runId) === currentRunId))
    );

    const coreReady = belongsToCurrentRun(core);
    const secondReady = belongsToCurrentRun(second);
    const performanceReady = belongsToCurrentRun(performance);

    let nextStage = '';
    if (coreReady && secondReady && performanceReady) {
      nextStage = 'complete';
    } else if (coreReady && secondReady) {
      nextStage = 'sending-performance';
    } else if (coreReady) {
      nextStage = 'sending-second';
    }

    if (!nextStage) return false;

    autoRuntime.stage = nextStage;
    autoRuntime.pausedReason = '';
    autoRuntime.pausedFromStage = '';
    autoRuntime.expectedKind = '';
    autoRuntime.waitStartedAt = nextStage === 'complete' ? 0 : Date.now();
    autoRuntime.stableResponseKey = '';
    autoRuntime.stableSince = 0;
    autoRuntime.anchorMissingSince = 0;
    clearAutoComposerHold();

    if (nextStage === 'complete') {
      autoRuntime.completeAt = autoRuntime.completeAt || Date.now();
    }

    if (!saveAutoRuntime({ pauseOnFailure: false })) {
      setStatus(
        'Auto3 found durable audit evidence for stale-PAUSE recovery, but runtime storage is temporarily unavailable. It will retry without restarting Core.',
        'warning'
      );
      scheduleAutoAuditCheck(1000);
      return true;
    }

    if (nextStage === 'complete') {
      setStatus(
        'Auto3 self-healed stale PAUSE from cached Core + Second + Performance results. Chain is COMPLETE.',
        'success'
      );
      renderAutoAuditState();
      return true;
    }

    const nextKind = nextStage === 'sending-second' ? 'second' : 'performance';
    setStatus(
      `Auto3 self-healed stale PAUSE from durable completed-wave evidence. Continuing automatically with ${waveLabel(nextKind)}.`,
      'success'
    );
    renderAutoAuditState();
    scheduleAutoAuditCheck(0);
    return true;
  }

  function recoverLegacySendRegistrationPauseFromDom() {
    if (!autoRuntime || autoRuntime.stage !== 'paused') return false;
    const reason = String(autoRuntime.pausedReason || '');
    if (!/Send was recorded, but its user turn is still absent|user turn is still absent/i.test(reason)) return false;

    const turns = getChatGPTTurns();
    const expectedKind = String(
      autoRuntime.expectedKind || autoRuntime.continuationKind ||
      (autoRuntime.pausedFromStage === 'await-second-user' ? 'second' : autoRuntime.pausedFromStage === 'await-performance-user' ? 'performance' : '')
    );
    const liveLineage = visibleAuditLineage(turns);
    if (liveLineage.blockedByReset) return false;
    const latestAudit = liveLineage.performance || liveLineage.second || liveLineage.core;
    const fallbackKind = expectedKind || classifyAuditTurn(latestAudit);
    if (!fallbackKind) return false;

    let matched = findPendingSentAuditTurn(fallbackKind, turns);
    if (matched && !autoRuntime.pendingSendReceipt) {
      const canonical = fallbackKind === 'core' ? liveLineage.core : fallbackKind === 'second' ? liveLineage.second : liveLineage.performance;
      const sameCanonical = canonical && (
        canonical === matched ||
        (getTurnId(canonical) && getTurnId(canonical) === getTurnId(matched))
      );
      if (!sameCanonical) matched = null;
    }

    if (!matched) {
      autoRuntime.pausedReason = 'Legacy send-registration state is ambiguous relative to the current visible audit lineage. Auto3 refuses heuristic adoption across a newer/virtualized root; explicit Reset or an exact modern receipt is required.';
      saveAutoRuntime({ pauseOnFailure: false });
      setStatus('Legacy send-registration recovery failed closed because lineage membership cannot be proven.', 'warning');
      return false;
    }

    const id = getTurnId(matched);
    autoRuntime.stage = waveWaitStage(fallbackKind);
    autoRuntime.pausedReason = '';
    autoRuntime.pausedFromStage = '';
    if (id) {
      setWaveUserId(fallbackKind, id);
      autoRuntime.seenUserId = id;
    }
    clearPendingSendReceipt({ save: false });
    autoRuntime.waitStartedAt = Date.now();
    autoRuntime.stableResponseKey = '';
    autoRuntime.stableSince = 0;
    if (!saveAutoRuntime()) return false;
    setStatus(`Recovered legacy registration only after canonical lineage validation. ${waveLabel(fallbackKind)} is already present; Auto3 resumed.`, 'success');
    scheduleAutoAuditCheck(0);
    return true;
  }

  function recoverLegacyPartialPauseFromDom() {
    if (!autoRuntime || autoRuntime.stage !== 'paused') return false;

    const reason = String(autoRuntime.pausedReason || '');
    if (!/reported\s+PARTIAL|PARTIAL.*audit protocol|automatic advancement.*PARTIAL/i.test(reason)) return false;

    const turns = getChatGPTTurns();
    const preferredStage = String(autoRuntime.pausedFromStage || '');
    const preferredKind = auditKindForStage(preferredStage);

    let userTurn = null;
    if (preferredKind) {
      for (let index = turns.length - 1; index >= 0; index -= 1) {
        const candidate = turns[index];
        if (turnRole(candidate) !== 'user') continue;
        if (classifyAuditTurn(candidate) === preferredKind) {
          userTurn = candidate;
          break;
        }
      }
    }

    if (!userTurn) {
      const lineage = visibleAuditLineage(turns);
      userTurn = lineage.blockedByReset ? null : (lineage.performance || lineage.second || lineage.core);
    }
    if (!userTurn) return false;

    const kind = classifyAuditTurn(userTurn);
    const stage = stageForAuditKind(kind);
    const assistant = assistantTurnAfter(userTurn, turns);
    if (!kind || !stage || !assistant || chatGPTIsGenerating()) return false;

    const proof = responseGateFromAssistantTurn(stage, assistant);
    if (proof.state !== 'partial' && proof.state !== 'complete') return false;

    const resumed = resumeRuntimeFromAuditTurn(userTurn, { turns });
    if (!resumed) return false;

    setStatus(
      proof.state === 'partial'
        ? `Recovered the old ${waveLabel(kind)} PARTIAL pause. Auto3 will continue the same wave automatically now.`
        : `Recovered the old ${waveLabel(kind)} pause; the existing response is already COMPLETE.`,
      'success'
    );
    scheduleAutoAuditCheck(0);
    return true;
  }

  function recoverStrictGatePauseFromDom() {
    if (!autoRuntime || autoRuntime.stage !== 'paused') return false;

    const reason = String(autoRuntime.pausedReason || '');
    if (!/complete marker|complete audit handoff|strict gate|response reported BLOCKED|reported BLOCKED/i.test(reason)) return false;

    const turns = getChatGPTTurns();
    const preferredStage = String(autoRuntime.pausedFromStage || '');
    const preferredKind = auditKindForStage(preferredStage);

    let userTurn = null;
    if (preferredKind) {
      for (let index = turns.length - 1; index >= 0; index -= 1) {
        if (turnRole(turns[index]) !== 'user') continue;
        if (classifyAuditTurn(turns[index]) === preferredKind) {
          userTurn = turns[index];
          break;
        }
      }
    }

    if (!userTurn) {
      const lineage = visibleAuditLineage(turns);
      userTurn = lineage.blockedByReset ? null : (lineage.performance || lineage.second || lineage.core);
    }
    if (!userTurn) return false;

    const kind = classifyAuditTurn(userTurn);
    const stage = stageForAuditKind(kind);
    const assistant = assistantTurnAfter(userTurn, turns);
    if (!stage || !assistant || chatGPTIsGenerating()) return false;

    const proof = responseGateFromAssistantTurn(stage, assistant);
    if (proof.state !== 'complete') return false;

    const resumed = resumeRuntimeFromAuditTurn(userTurn, { turns });
    if (resumed) {
      setStatus(`Recovered a false Strict pause directly from the live DOM: ${kind === 'core' ? 'Core' : kind === 'second' ? 'Second Wave' : 'Performance'} is COMPLETE.`, 'success');
    }
    return resumed;
  }

  // The MutationObserver is the automation's eyes on the live DOM. Its payload
  // is scaled to what the current state actually needs, because token streaming
  // is the most mutation-heavy surface on a ChatGPT page:
  //   'stream' - enabled, waiting for or sending a wave: full subtree watching
  //              including characterData, so streamed text progress is seen;
  //   'turns'  - enabled, idle/complete/paused: childList only (no characterData
  //              flood); new turn insertions and root replacements still seen;
  //   'nav'    - disabled: childList only, binding/root tracking, no scheduling.
  // Config transitions (disable, pause, complete, wave send, root replacement)
  // recreate the observer so the heavy config never outlives the state that
  // needs it.
  function autoAuditObserverConfig() {
    if (detectSite().key !== 'chatgpt') return null;
    if (!autoRuntime?.enabled) return 'nav';
    return ['wait-core', 'wait-second', 'wait-performance',
      'sending-second', 'sending-performance', 'sending-continuation',
      'await-second-user', 'await-performance-user', 'await-continuation-user'
    ].includes(autoRuntime.stage)
      ? 'stream'
      : 'turns';
  }

  function ensureAutoAuditObserver() {
    const config = autoAuditObserverConfig();
    if (!config) {
      if (autoAuditObserver) {
        autoAuditObserver.disconnect();
        autoAuditObserver = null;
      }
      autoAuditObserverRoot = null;
      autoAuditObservedConfig = null;
      return;
    }

    const root = document.querySelector('main') || document.body;
    if (!root) return;

    const wasBound = Boolean(autoAuditObserver && autoAuditObserverRoot);
    const rootChanged = wasBound && autoAuditObserverRoot !== root;
    if (wasBound && !rootChanged && autoAuditObserverRoot.isConnected && autoAuditObservedConfig === config) return;

    if (autoAuditObserver) autoAuditObserver.disconnect();
    autoAuditObserver = null;
    autoAuditObserverRoot = root;
    autoAuditObservedConfig = config;

    autoAuditObserver = new MutationObserver(records => {
      // Root re-anchor must happen before any mutation filtering. When ChatGPT
      // replaces <main> (SPA route change), the observed root detaches and the
      // old observer silently dies; detect it up front and rebind so the Auto3
      // monitor stays bound to the live root through the transition.
      const liveRoot = document.querySelector('main') || document.body;
      if (liveRoot !== autoAuditObserverRoot) {
        ensureAutoAuditObserver();
      }

      const external = externalMutationRecords(records);

      // Critical hot-loop guard: renderAutoAuditState() mutates ACB controls.
      // If the observer root is document.body (or ChatGPT moves our panel under
      // the observed root), those mutations must be invisible to automation.
      if (!external.length) return;

      // Streamed assistant tokens arrive predominantly as characterData. They
      // need one trailing audit debounce, not route binding, title repair,
      // attachment rediscovery, or other topology work on every token batch.
      const characterDataOnly = external.every(record => record.type === 'characterData');
      if (characterDataOnly) {
        if (autoComposerHoldReason && autoComposerHoldApplicable()) {
          scheduleAutoComposerHoldProbe(autoComposerHoldKind(), { force: true, delay: 140 });
        }
        if (autoRuntime?.enabled && autoAuditObserverConfig() === 'stream') {
          scheduleAutoAuditCheck(AUTO_OBSERVER_DEBOUNCE_MS);
        }
        return;
      }

      // Topology mutations can replace/detach the observed root. Re-anchor only
      // for those mutations, never for ordinary streamed text.
      ensureAutoAuditObserver();

      const previousKey = autoBoundConversationKey;
      bindAutoRuntimeToCurrentConversation({ claim: false });
      const conversationChanged = previousKey !== autoBoundConversationKey;

      if (conversationChanged) {
        miniAttachmentSignature = '';
        renderAutoAuditState();
      }

      if (autoRuntime?.projectName && state?.autoRenameChat) {
        applyPersistentLocalProjectTitle(autoRuntime.projectName);
      }

      // Mini START is attachment-driven. Repaint only if a composer mutation
      // actually changes attachment presence/name/busy state.
      scheduleMiniAttachmentRefresh(external);

      if (!autoRuntime?.enabled) return;

      const configNow = autoAuditObserverConfig();

      if (autoComposerHoldReason && autoComposerHoldApplicable()) {
        scheduleAutoComposerHoldProbe(autoComposerHoldKind(), {
          force: true,
          delay: 140
        });
      }

      if (configNow === 'stream') {
        // Active audit responses still need the normal trailing debounce.
        scheduleAutoAuditCheck(AUTO_OBSERVER_DEBOUNCE_MS);
        return;
      }

      // Idle/complete/paused does not need full response parsing for every React
      // childList mutation. Only a likely conversation-turn insertion/removal or
      // a conversation identity change can matter here.
      const turnMutation = external.some(record => {
        const nodes = [
          acbElementFromNode(record.target),
          ...Array.from(record.addedNodes || []).map(acbElementFromNode),
          ...Array.from(record.removedNodes || []).map(acbElementFromNode)
        ].filter(Boolean);

        return nodes.some(node => {
          if (node.matches?.('[data-message-author-role], [data-testid^="conversation-turn-"]')) return true;
          if (node.closest?.('[data-message-author-role], [data-testid^="conversation-turn-"]')) return true;
          if (node.querySelector?.('[data-message-author-role], [data-testid^="conversation-turn-"]')) return true;
          return false;
        });
      });

      if (conversationChanged || turnMutation) {
        scheduleAutoAuditCheck(AUTO_OBSERVER_DEBOUNCE_MS);
      }
    });
    autoAuditObserver.observe(root, {
      childList: true,
      subtree: true,
      characterData: config === 'stream'
    });

    // Exactly one evaluation after a re-bind on a NEW root: mutations that
    // happened while the observer was detached were not seen, so re-sync the
    // chain once against the live conversation. Config-only transitions skip
    // the evaluation because the in-memory stage already moved.
    if (rootChanged && autoRuntime?.enabled) {
      evaluateAutoAudit().catch(error => pauseAutoAudit(`Monitor re-bind failed: ${error?.message || 'unexpected runtime error'}.`));
    }
  }

  function startAutoAuditMonitor(options = {}) {
    if (detectSite().key !== 'chatgpt') {
      renderAutoAuditState();
      return;
    }

    bindAutoRuntimeToCurrentConversation({ claim: false });
    if (chatGPTRootIsQuarantined() || autoBoundConversationKey?.startsWith('auth:')) {
      clearAutoTimers();
      releaseAutoLease(lastStableConversationKey());
      renderAutoAuditState();
      ensureAutoAuditObserver();
      scheduleAutoAuditCheck(AUTO_AUTH_HOLD_RETRY_MS);
      return;
    }

    ensureAutoAuditObserver();
    const pendingStart = readStartAuditHandoff();
    if (startHandoffIsCommitted(pendingStart) && !autoRuntime?.enabled) {
      // Enable only in memory so claimAutoLease can establish write authority;
      // durable recovery follows only after that claim is verified.
      if (!autoRuntime) autoRuntime = emptyAutoRuntime();
      autoRuntime.enabled = true;
      autoRuntime.conversationKey = autoBoundConversationKey || currentConversationKey();
    }

    if (!autoRuntime?.enabled) {
      clearAutoTimers();
      releaseAutoLease(autoBoundConversationKey);
      renderAutoAuditState();
      return;
    }
    if (autoRuntime.stage === 'paused' && pauseIsExplicitHumanStop(autoRuntime.pausedReason)) {
      releaseAutoLease(autoBoundConversationKey);
      renderAutoAuditState();
      return;
    }

    const ownsStartupLease = claimAutoLease();
    if (!ownsStartupLease) {
      // Standby tabs may observe/render, but must not reconcile or persist shared
      // runtime until ownership transfers to them.
      refreshAutoRuntimeFromStorage();
      renderAutoAuditState();
      scheduleAutoAuditCheck(options.immediate ? 350 : 900);
      return;
    }

    adoptA3IntentForConversation(autoBoundConversationKey || currentConversationKey());
    if (startHandoffIsCommitted(pendingStart)) {
      recoverSentStartCore({ source: 'monitor-bootstrap', skipMonitor: true });
      writeA3Intent(true, autoBoundConversationKey || currentConversationKey(), { startTransaction: true });
      adoptA3IntentForConversation(autoBoundConversationKey || currentConversationKey(), { startTransaction: true });
      scheduleSentStartRecovery();
    }

    const startupTurns = getChatGPTTurns();
    recoverCommittedSendFromDom(startupTurns);
    recoverLegacySendRegistrationPauseFromDom();
    recoverLegacyPartialPauseFromDom();
    recoverStrictGatePauseFromDom();
    recoverStalePauseFromConversation(startupTurns, { source: 'startup' });

    if (autoRuntime.enabled && autoRuntime.stage === 'idle' && reconcileEnabledIdleAuditRuntime(startupTurns, { source: 'monitor-startup' })) {
      renderAutoAuditState();
      return;
    }

    if (autoRuntime.stage === 'idle' && !autoRuntime.seenUserId) {
      const latestUser = latestChatGPTUserTurn(startupTurns);
      autoRuntime.seenUserId = getTurnId(latestUser);
      autoRuntime.baselineAssistantKey = assistantFingerprint(latestUser ? assistantTurnAfter(latestUser, startupTurns) : null);
      saveAutoRuntime({ pauseOnFailure: false });
    }

    renderAutoAuditState();
    scheduleAutoAuditCheck(options.immediate ? 0 : 250);
  }

  function stopAutoAuditMonitor() {
    if (autoAuditObserver) {
      autoAuditObserver.disconnect();
      autoAuditObserver = null;
    }
    autoAuditObserverRoot = null;
    clearAutoTimers();
    releaseAutoLease(autoBoundConversationKey);
  }

  function setAutoAuditEnabled(enabled) {
    bindAutoRuntimeToCurrentConversation({ claim: false });

    const next = Boolean(enabled);
    if (!autoRuntime) autoRuntime = emptyAutoRuntime({ enabled: next });

    const previous = autoRuntime.enabled;
    autoRuntime.enabled = next;
    autoRuntime.conversationKey = autoBoundConversationKey || currentConversationKey();

    if (next) {
      writeA3Intent(true, autoRuntime.conversationKey, {
        startTransaction: Boolean(startHandoffIsCommitted(readStartAuditHandoff()))
      });
    } else {
      clearA3Intent();
      clearStartAuditHandoff();
    }

    if (!saveAutoRuntime()) {
      autoRuntime.enabled = previous;
      if (previous) writeA3Intent(true, autoRuntime.conversationKey);
      else clearA3Intent();
      renderAutoAuditState();
      return;
    }

    if (!next) {
      clearAutoComposerHold();
      clearAutoTimers();
      releaseAutoLease(autoBoundConversationKey);
      ensureAutoAuditObserver();
      setStatus('Auto 3 waves disabled for this chat only. Saved progress is preserved; other and future conversations are unaffected.', 'success');
      renderAutoAuditState();
      return;
    }

    claimAutoLease();
    startAutoAuditMonitor({ immediate: true });

    const turns = getChatGPTTurns();
    if (autoRuntime.stage === 'idle') {
      reconcileEnabledIdleAuditRuntime(turns, { source: 'explicit-enable' });
    }

    evaluateAutoAudit().catch(error => pauseAutoAudit(`Monitor start failed: ${error?.message || 'unexpected runtime error'}.`));

    if (autoRuntime.stage === 'idle') {
      setStatus('Auto 3 waves enabled for this chat. No resumable audit lineage is currently proven; waiting for Audit Core.', 'success');
    }
  }

  function renderConfirm() {
    const text = panel?.querySelector('#acb-confirm-text');
    const cancel = panel?.querySelector('#acb-confirm-cancel');
    const confirm = panel?.querySelector('#acb-confirm-run');
    if (!text || !cancel || !confirm) return;

    if (!pendingAction) {
      text.textContent = 'Nothing pending.';
      cancel.disabled = true;
      confirm.disabled = true;
      cancel.title = 'No destructive action is pending.';
      confirm.title = 'No destructive action is pending.';
      return;
    }

    text.textContent = pendingAction.message;
    cancel.disabled = false;
    confirm.disabled = false;
    cancel.title = 'Cancel the pending destructive action.';
    confirm.title = 'Perform the exact action described above.';
  }

  function clearPendingAction() {
    pendingAction = null;
    renderConfirm();
  }

  function readCategoryName() {
    return String(panel?.querySelector('#acb-category-name')?.value || '').trim().slice(0, 30);
  }

  function addCategory() {
    if (state.categories.length >= MAX_CATEGORIES) {
      setStatus(`Category was not added: the ${MAX_CATEGORIES}-category limit is reached. Delete an unused category first.`, 'error');
      return;
    }
    const name = readCategoryName();
    if (!name) {
      setStatus('Category was not added: Category name is empty. Enter a name in the labeled field, then press Add.', 'error');
      return;
    }
    if (state.categories.some(category => category.name.toLowerCase() === name.toLowerCase())) {
      setStatus(`Category was not added: "${name}" already exists. Enter a unique category name, then press Add.`, 'error');
      return;
    }
    clearPendingAction();
    const category = { id: uid(), name, presets: [] };
    if (!commitStateMutation(() => {
      state.categories.push(category);
      state.activeCategoryId = category.id;
    }, 'New category could not be persisted; it was not added.')) return;
    renderCategoryTabs();
    renderCommands();
    renderManageCategory();
    renderManageList();
    hideEditor();
    setStatus(`Added category: ${category.name}.`, 'success');
  }

  function renameCategory() {
    const category = activeCategory();
    if (!category) return;
    const name = readCategoryName();
    if (!name) {
      setStatus('Category was not renamed: Category name is empty. Enter a name in the labeled field, then press Rename.', 'error');
      return;
    }
    if (state.categories.some(item => item.id !== category.id && item.name.toLowerCase() === name.toLowerCase())) {
      setStatus(`Category was not renamed: "${name}" already exists. Enter a unique category name, then press Rename.`, 'error');
      return;
    }
    clearPendingAction();
    const oldName = category.name;
    if (!commitStateMutation(
      () => { category.name = name; },
      'Category rename could not be persisted; the previous name was restored.'
    )) return;
    renderCategoryTabs();
    renderManageCategory();
    setStatus(`Renamed category: ${oldName} -> ${category.name}.`, 'success');
  }

  function requestDeleteCategory() {
    const category = activeCategory();
    if (!category) return;
    if (state.categories.length <= 1) {
      setStatus('Category was not queued for deletion: at least one category must remain. Add another category first.', 'error');
      return;
    }
    pendingAction = {
      type: 'delete-category',
      categoryId: category.id,
      message: `Delete category "${category.name}" and its ${category.presets.length} command(s)? Confirm removes them from AI ChatButtons.`
    };
    renderConfirm();
    setStatus(`Deletion pending for category: ${category.name}. Read Confirm action, then Confirm or Cancel.`, 'warning');
  }

  function requestDeletePreset(presetId) {
    const category = activeCategory();
    const preset = category?.presets.find(item => item.id === presetId);
    if (!category || !preset) {
      setStatus('Command was not queued for deletion: it no longer exists in the selected category. Refresh by reselecting the category.', 'error');
      return;
    }
    pendingAction = {
      type: 'delete-preset',
      categoryId: category.id,
      presetId: preset.id,
      message: `Delete command "${preset.name}" from category "${category.name}"? Confirm permanently removes this command from AI ChatButtons.`
    };
    renderConfirm();
    setStatus(`Deletion pending for command: ${preset.name}. Read Confirm action, then Confirm or Cancel.`, 'warning');
  }

  function movePreset(presetId, delta) {
    const category = activeCategory();
    if (!category) return;
    const index = category.presets.findIndex(item => item.id === presetId);
    const next = index + delta;
    if (index < 0) {
      setStatus('Command was not moved: it no longer exists in this category. Reselect the category and retry.', 'error');
      return;
    }
    if (next < 0 || next >= category.presets.length) {
      setStatus(`Command was not moved: ${category.presets[index].name} is already ${delta < 0 ? 'first' : 'last'} in ${category.name}.`, 'warning');
      return;
    }
    clearPendingAction();
    const presetName = category.presets[index].name;
    if (!commitStateMutation(() => {
      const [moved] = category.presets.splice(index, 1);
      category.presets.splice(next, 0, moved);
    }, 'Command order could not be persisted; the previous order was restored.')) return;
    renderCommands();
    renderManageList();
    setStatus(`Moved ${presetName} ${delta < 0 ? 'up' : 'down'} in ${category.name}.`, 'success');
  }

  function exportPresets() {
    const payload = {
      version: 2,
      exportedAt: new Date().toISOString(),
      categories: state.categories
    };
    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = 'AICHATBUTTONS_presets.json';
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    URL.revokeObjectURL(url);
    const count = state.categories.reduce((sum, category) => sum + category.presets.length, 0);
    setStatus(`Exported ${count} command(s) from ${state.categories.length} category(s).`, 'success');
  }

  function beginImport() {
    if (!fileInput) return;
    fileInput.value = '';
    fileInput.click();
  }

  function findDuplicateName(categories) {
    const categoryNames = new Set();
    for (const category of categories) {
      const categoryKey = category.name.toLowerCase();
      if (categoryNames.has(categoryKey)) return `duplicate category name "${category.name}"`;
      categoryNames.add(categoryKey);
      const presetNames = new Set();
      for (const preset of category.presets) {
        const presetKey = preset.name.toLowerCase();
        if (presetNames.has(presetKey)) return `duplicate command name "${preset.name}" in category "${category.name}"`;
        presetNames.add(presetKey);
      }
    }
    return '';
  }

  function handleImportFile(event) {
    const file = event.target.files?.[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
      try {
        const parsed = JSON.parse(String(reader.result || ''));
        if (!Array.isArray(parsed?.categories)) {
          setStatus('Import rejected: JSON is missing the categories array. Export a valid preset file and retry.', 'error');
          return;
        }
        if (parsed.categories.length > MAX_CATEGORIES) {
          setStatus(`Import rejected: file has ${parsed.categories.length} categories, above the ${MAX_CATEGORIES}-category limit. Reduce the file and retry.`, 'error');
          return;
        }
        const oversizedCategory = parsed.categories.find(category => Array.isArray(category?.presets) && category.presets.length > MAX_PRESETS);
        if (oversizedCategory) {
          setStatus(`Import rejected: category "${String(oversizedCategory.name || 'unnamed')}" exceeds the ${MAX_PRESETS}-command limit. Reduce it and retry.`, 'error');
          return;
        }
        const categories = sanitizeCategories(parsed.categories);
        if (!categories.length) {
          setStatus('Import rejected: JSON contains no valid categories with the required structure. Export a valid preset file and retry.', 'error');
          return;
        }
        const duplicate = findDuplicateName(categories);
        if (duplicate) {
          setStatus(`Import rejected: ${duplicate}. Rename the duplicate in the JSON file and retry.`, 'error');
          return;
        }
        const commandCount = categories.reduce((sum, category) => sum + category.presets.length, 0);
        pendingAction = {
          type: 'import-replace',
          categories,
          commandCount,
          message: `Replace all current data with ${categories.length} imported category(s) and ${commandCount} command(s)? Confirm removes the current categories first.`
        };
        renderConfirm();
        setStatus('Import file is valid but not applied. Read Confirm action, then Confirm or Cancel.', 'warning');
      } catch (error) {
        setStatus(`Import rejected: ${error.message || 'invalid JSON'}. Fix the preset file and retry.`, 'error');
      }
    };
    reader.onerror = () => {
      setStatus('Import failed: the selected file could not be read. Check file permissions and retry.', 'error');
    };
    reader.readAsText(file);
  }

  function confirmPendingAction() {
    if (!pendingAction) {
      setStatus('Nothing was confirmed: no destructive action is pending.', 'warning');
      return;
    }

    const action = pendingAction;
    pendingAction = null;

    if (action.type === 'delete-category') {
      const category = state.categories.find(item => item.id === action.categoryId);
      if (!category) {
        renderConfirm();
        setStatus('Category was not deleted: the pending category no longer exists. Select a current category and request deletion again.', 'error');
        return;
      }
      if (state.categories.length <= 1) {
        renderConfirm();
        setStatus('Category was not deleted: it became the last remaining category. Add another category first.', 'error');
        return;
      }
      if (!commitStateMutation(() => {
        state.categories = state.categories.filter(item => item.id !== category.id);
        if (!state.categories.some(item => item.id === state.activeCategoryId)) {
          state.activeCategoryId = state.categories[0].id;
        }
      }, 'Category deletion could not be persisted; nothing was deleted.')) {
        renderConfirm();
        return;
      }
      renderCategoryTabs();
      renderCommands();
      renderManageCategory();
      renderManageList();
      hideEditor();
      renderConfirm();
      setStatus(`Deleted category: ${category.name}.`, 'success');
      return;
    }

    if (action.type === 'delete-preset') {
      const category = state.categories.find(item => item.id === action.categoryId);
      const preset = category?.presets.find(item => item.id === action.presetId);
      if (!category || !preset) {
        renderConfirm();
        setStatus('Command was not deleted: the pending command no longer exists. Reselect the category and request deletion again.', 'error');
        return;
      }
      if (!commitStateMutation(
        () => { category.presets = category.presets.filter(item => item.id !== preset.id); },
        'Command deletion could not be persisted; nothing was deleted.'
      )) {
        renderConfirm();
        return;
      }
      renderCommands();
      renderManageList();
      if (editingPresetId === preset.id) hideEditor();
      renderConfirm();
      setStatus(`Deleted command: ${preset.name}.`, 'success');
      return;
    }

    if (action.type === 'import-replace') {
      const committed = commitStateMutation(() => {
        state.categories = action.categories.map(category => ({
          id: uid(),
          name: category.name,
          presets: category.presets.map(preset => ({
            id: uid(),
            ...(canonicalBuiltinId(preset.builtinId) ? { builtinId: canonicalBuiltinId(preset.builtinId) } : {}),
            name: preset.name,
            desc: preset.desc,
            text: preset.text
          }))
        }));
        state.activeCategoryId = state.categories[0].id;
        state.builtinRevision = 0;
        state.builtinsSeededV2 = false;
        syncBuiltins(state);
      }, 'Import could not be persisted; the previous preset library was restored.');
      if (!committed) {
        renderConfirm();
        return;
      }
      renderCategoryTabs();
      renderCommands();
      renderManageCategory();
      renderManageList();
      hideEditor();
      renderConfirm();
      setStatus(`Imported ${action.commandCount} command(s) and reconciled canonical audit built-ins.`, 'success');
      return;
    }

    renderConfirm();
    setStatus('Pending action was not executed: its type is unsupported. Request the action again from the current UI.', 'error');
  }

  function attachEvents() {
    panel.querySelector('#acb-new-chat').addEventListener('click', event => {
      event.preventDefault();
      event.stopPropagation();
      openNewChatFromWidget();
    });

    panel.querySelector('#acb-super-toggle').addEventListener('click', () => {
      const next = !state.superCompact;
      if (!commitStateMutation(
        () => {
          state.superCompact = next;
          if (next) state.collapsed = false;
        },
        'Super compact display mode could not be persisted; the previous display state was restored.'
      )) return;

      clampPanelPosition({ report: true });
      renderAutoAuditState();

      if (!next) {
        setStatus('Full widget restored.', 'success');
      }
    });

    panel.querySelector('#acb-collapse').addEventListener('click', () => {
      if (!commitStateMutation(
        () => {
          state.superCompact = false;
          state.collapsed = !state.collapsed;
        },
        'Collapse state could not be persisted; the previous display state was restored.'
      )) return;
      clampPanelPosition({ report: true });
      if (!state.collapsed) {
        setStatus('Widget expanded. Display state restored.', 'success');
      }
    });

    panel.querySelector('#acb-opacity').addEventListener('change', event => {
      const next = Number(event.target.value);
      if (!OPACITY_LEVELS.includes(next)) return;
      if (!commitStateMutation(
        () => { state.opacity = next; },
        'Opacity could not be persisted; the previous value was restored.'
      )) return;
      applyDisplayState();
      setStatus(`Widget opacity set to ${next}%.`, 'success');
    });

    panel.querySelector('#acb-size').addEventListener('change', event => {
      const next = String(event.target.value);
      if (!Object.prototype.hasOwnProperty.call(PANEL_SIZES, next)) return;
      if (!commitStateMutation(
        () => { state.panelSize = next; },
        'Widget size could not be persisted; the previous size was restored.'
      )) return;
      clampPanelPosition({ report: true });
      setStatus(`Widget size set to ${PANEL_SIZES[next].label}.`, 'success');
    });

    panel.querySelector('#acb-lock').addEventListener('click', () => {
      const nextLocked = !state.posLocked;
      if (!commitStateMutation(() => {
        if (nextLocked) clampPanelPosition({ commit: true });
        state.posLocked = nextLocked;
      }, 'Position lock state could not be persisted; the previous state was restored.')) return;
      updateLockState();
      setStatus(`Position ${state.posLocked ? 'locked' : 'unlocked'}.`, 'success');
    });

    const handleAuditStepActivation = event => {
      const step = event.target.closest('[data-step]');
      if (!step) return;
      if (event.type === 'keydown' && !['Enter', ' '].includes(event.key)) return;
      if (event.type === 'keydown') event.preventDefault();
      const kind = auditKindFromStep(step.dataset.step);
      if (kind) copyCompletedAudit(kind).catch(error => {
        setStatus(`Audit copy failed: ${error?.message || 'unexpected clipboard error'}.`, 'error');
      });
    };
    panel.querySelector('#acb-auto-progress').addEventListener('click', handleAuditStepActivation);
    panel.querySelector('#acb-auto-progress').addEventListener('keydown', handleAuditStepActivation);
    panel.querySelector('#acb-super-progress').addEventListener('click', handleAuditStepActivation);
    panel.querySelector('#acb-super-progress').addEventListener('keydown', handleAuditStepActivation);

    panel.querySelector('#acb-auto-enabled').addEventListener('change', event => {
      setAutoAuditEnabled(event.target.checked);
    });

    panel.querySelector('#acb-super-enabled').addEventListener('change', event => {
      setAutoAuditEnabled(event.target.checked);
    });

    panel.querySelector('#acb-save-now').addEventListener('click', () => {
      syncSaveCurrentChatStateNow().catch(error => {
        manualAuditSyncInFlight = false;
        setStatus(`SYNC/SAVE failed: ${error?.message || 'unexpected persistence error'}. Cached audits and runtime state were not intentionally discarded.`, 'error');
        renderAutoAuditState();
      });
    });

    panel.querySelector('#acb-super-state').addEventListener('click', event => {
      const action = String(event.currentTarget?.dataset?.action || 'none');

      if (action === 'start-audit') {
        startAuditCoreFromReadyAttachment().catch(error => {
          setStatus(`START AUDITING failed: ${error?.message || 'unexpected Core launch error'}.`, 'error');
          auditStartInFlight = false;
          renderAutoAuditState();
        });
        return;
      }

      if (action === 'sync-save') {
        syncSaveCurrentChatStateNow().catch(error => {
          manualAuditSyncInFlight = false;
          setStatus(`SYNC/SAVE failed: ${error?.message || 'unexpected persistence error'}. Cached audits and runtime state were not intentionally discarded.`, 'error');
          renderAutoAuditState();
        });
      }
    });

    panel.querySelector('#acb-auto-gate').addEventListener('change', event => {
      const next = event.target.value !== 'relaxed';
      if (!commitStateMutation(
        () => { state.autoAuditStrictGate = next; },
        'Auto audit gate setting could not be persisted; the previous value was restored.'
      )) return;
      renderAutoAuditState();
      setStatus(`Auto audit gate set to ${state.autoAuditStrictGate ? 'Strict' : 'Relaxed'}.`, 'success');
      scheduleAutoAuditCheck(0);
    });

    panel.querySelector('#acb-auto-delay').addEventListener('change', event => {
      const next = Number(event.target.value);
      if (!AUTO_DELAYS_MS.includes(next)) return;
      if (!commitStateMutation(
        () => { state.autoAuditDelayMs = next; },
        'Auto audit delay could not be persisted; the previous value was restored.'
      )) return;
      renderAutoAuditState();
      setStatus(`Auto audit next-wave delay set to ${next / 1000} s.`, 'success');
    });

    panel.querySelector('#acb-auto-timeout').addEventListener('change', event => {
      const next = Number(event.target.value);
      if (!AUTO_STAGE_TIMEOUTS.includes(next)) return;
      if (!commitStateMutation(
        () => { state.autoAuditTimeoutMin = next; },
        'Auto audit timeout could not be persisted; the previous value was restored.'
      )) return;
      renderAutoAuditState();
      setStatus(`Auto audit maximum wait set to ${next} minutes per wave.`, 'success');
    });


    panel.querySelector('#acb-prompt-delivery').addEventListener('change', event => {
      const next = String(event.target.value);
      if (!CHATGPT_PROMPT_DELIVERY_MODES.includes(next)) return;
      if (!commitStateMutation(
        () => { state.chatgptPromptDelivery = next; },
        'Prompt delivery setting could not be persisted; the previous value was restored.'
      )) return;
      renderAutoAuditState();
      const label = next === 'auto'
        ? `Auto file for prompts >= ${CHATGPT_LONG_PROMPT_THRESHOLD} characters`
        : next === 'file'
          ? 'File attachment for every ChatGPT command'
          : 'Raw text insertion';
      setStatus(`ChatGPT prompt delivery: ${label}.`, 'success');
    });

    panel.querySelector('#acb-auto-rename-chat').addEventListener('change', event => {
      const next = Boolean(event.target.checked);
      if (!commitStateMutation(
        () => { state.autoRenameChat = next; },
        'Chat auto-rename setting could not be persisted; the previous value was restored.'
      )) return;
      renderAutoAuditState();
      setStatus(`Chat auto-rename ${next ? 'enabled' : 'disabled'}.`, 'success');
      if (next && autoRuntime?.projectName) {
        const renameContext = {
          source: autoRuntime.projectNameSource || 'artifact',
          conversationKey: currentConversationKey(),
          runStartedAt: autoRuntime.startedAt
        };
        maybeRenameConversation(autoRuntime.projectName, renameContext).catch(() => { });
        scheduleConversationTitleGuard(autoRuntime.projectName, renameContext);
      } else if (!next) {
        conversationTitleGuardToken = '';
        conversationTitleGuardStartedAt = 0;
        conversationTitleGuardRunStartedAt = 0;
      }
    });

    panel.querySelector('#acb-bridge-enabled').addEventListener('change', event => {
      const next = Boolean(event.target.checked);
      if (!commitStateMutation(
        () => { state.bridgeEnabled = next; },
        'Bridge enablement could not be persisted; the previous value was restored.'
      )) return;

      bridgeState = next ? 'unknown' : 'disabled';
      bridgeMessage = next ? 'Bridge enabled; checking localhost service.' : 'Bridge integration disabled; browser-folder fallback is available.';
      renderAutoAuditState();

      if (next) {
        checkBridge({ force: true }).catch(() => { });
      } else {
        clearBridgeFlushTimer();
      }
    });

    panel.querySelector('#acb-bridge-url').addEventListener('change', event => {
      const candidate = String(event.target.value || '').trim();
      const normalized = normalizedBridgeUrl(candidate);
      if (!normalized) {
        event.target.value = state.bridgeUrl || BRIDGE_DEFAULT_URL;
        setStatus('Bridge URL rejected. Only loopback HTTP URLs such as http://127.0.0.1:17843 are allowed.', 'error');
        return;
      }

      if (!commitStateMutation(
        () => { state.bridgeUrl = normalized; },
        'Bridge URL could not be persisted; the previous value was restored.'
      )) return;

      bridgeState = 'unknown';
      bridgeMessage = 'Bridge URL changed; run Check + flush.';
      renderBridgeState();
      if (state.bridgeEnabled) checkBridge({ force: true }).catch(() => { });
    });

    panel.querySelector('#acb-bridge-save-token').addEventListener('click', () => {
      const input = panel.querySelector('#acb-bridge-token');
      const value = String(input?.value || '').trim();
      if (!value) {
        setStatus(bridgeToken()
          ? 'A bridge token is already stored. Paste a replacement only if you want to rotate it.'
          : 'Paste the AUDAPACK Bridge token first.', bridgeToken() ? 'info' : 'warning');
        return;
      }

      if (!saveBridgeToken(value)) return;
      input.value = '';
      input.placeholder = 'Token stored · paste only to replace';
      resetBridgeFailedJobs('invalid_auth');
      bridgeState = 'unknown';
      bridgeMessage = 'Token saved; checking bridge.';
      renderBridgeState();
      setStatus('AUDAPACK Bridge token saved locally in Tampermonkey storage. It is not placed in prompts, filenames, or preset exports.', 'success');
      checkBridge({ force: true }).catch(() => { });
    });

    panel.querySelector('#acb-bridge-check').addEventListener('click', () => {
      checkBridge({ force: true })
        .then(ok => {
          if (ok) setStatus('AUDAPACK Bridge connected. Queued audit saves are being flushed.', 'success');
          else setStatus(`AUDAPACK Bridge check did not reach writable authenticated state: ${bridgeMessage}`, 'warning');
        })
        .catch(error => setStatus(`AUDAPACK Bridge check failed: ${error?.message || 'unexpected bridge error'}.`, 'error'));
    });

    panel.querySelector('#acb-choose-audit-folder').addEventListener('click', () => {
      chooseAuditOutputFolder().catch(error => setStatus(`Audit folder selection failed: ${error?.message || 'unexpected error'}.`, 'error'));
    });

    panel.querySelector('#acb-flush-audit-files').addEventListener('click', () => {
      flushCurrentAuditResultsToFolder({ force: true })
        .then(result => {
          if (!result.ready) {
            setStatus('No completed audit wave is cached in this chat yet.', 'info');
          } else if (result.saved === result.ready) {
            setStatus(`Saved ${result.saved}/${result.ready} cached wave(s)${result.combined ? ' plus ALL_3' : ''} to the linked audit folder.`, 'success');
          } else {
            setStatus(`Saved ${result.saved}/${result.ready} cached wave(s). Folder permission/state still needs attention for the rest.`, 'warning');
          }
        })
        .catch(error => setStatus(`Cached audit save failed: ${error?.message || 'unexpected file error'}.`, 'error'));
    });

    panel.querySelector('#acb-auto-reset').addEventListener('click', () => {
      resetAutoAuditRuntime();
      if (autoRuntime?.enabled) startAutoAuditMonitor({ immediate: true });
    });

    panel.querySelector('#acb-auto-adopt').addEventListener('click', () => {
      if (!autoRuntime?.enabled) {
        setStatus('Enable Auto 3 waves for this chat before Resume.', 'warning');
        return;
      }
      try {
        resumeAutoAuditFromConversation();
      } catch (error) {
        pauseAutoAudit(`Could not rebuild the current audit chain from the live conversation: ${error?.message || 'unexpected runtime error'}.`);
      }
    });

    panel.querySelector('#acb-auto-stop').addEventListener('click', () => {
      if (!autoRuntime?.enabled) {
        setStatus('Auto 3 waves is already disabled for this chat.', 'info');
        return;
      }
      pauseAutoAudit('Paused manually from the widget.');
    });

    panel.querySelector('#acb-tabs').addEventListener('click', event => {
      const button = event.target.closest('button[data-view]');
      if (!button) return;
      activeView = button.dataset.view;
      renderTabs();
      if (activeView === 'manage') {
        renderManageCategory();
        renderManageList();
        renderConfirm();
      }
      const viewLabel = activeView === 'commands' ? 'Run' : activeView === 'manage' ? 'Edit' : 'Settings';
      setStatus(`Opened ${viewLabel}.`, 'info');
    });

    panel.querySelector('#acb-filter').addEventListener('input', renderCommands);

    panel.querySelector('#acb-audit-quick-list').addEventListener('click', event => {
      const button = event.target.closest('button[data-quick-action]');
      const row = button?.closest('.acb-audit-quick-row');
      if (!button || !row) return;

      const preset = findAuditPreset(row.dataset.wave);
      if (!preset) {
        setStatus('This built-in audit command is missing. Open Edit or reload defaults before running it.', 'error');
        renderAuditQuickActions();
        return;
      }

      executePreset(preset, button.dataset.quickAction);
    });

    panel.querySelector('#acb-command-list').addEventListener('click', event => {
      const button = event.target.closest('button[data-action]');
      const row = button?.closest('.acb-command-row');
      if (!button || !row) return;
      const category = activeCategory();
      const preset = category?.presets.find(item => item.id === row.dataset.presetId);
      if (!preset) {
        setStatus('Command action failed: the selected command no longer exists. Refresh the list by switching categories.', 'error');
        return;
      }
      executePreset(preset, button.dataset.action);
    });

    panel.querySelector('#acb-manage-category').addEventListener('change', event => {
      const nextCategoryId = event.target.value;
      if (!commitStateMutation(
        () => { state.activeCategoryId = nextCategoryId; },
        'Category selection could not be persisted; the previous selection was restored.'
      )) return;
      renderCategoryTabs();
      renderCommands();
      renderManageCategory();
      renderManageList();
      hideEditor();
      const category = activeCategory();
      setStatus(`Selected category: ${category?.name || 'unknown'}.`, 'info');
    });

    panel.querySelector('#acb-add-category').addEventListener('click', addCategory);
    panel.querySelector('#acb-rename-category').addEventListener('click', renameCategory);
    panel.querySelector('#acb-delete-category').addEventListener('click', requestDeleteCategory);
    panel.querySelector('#acb-add-command').addEventListener('click', () => showEditor(null));
    panel.querySelector('#acb-editor-save').addEventListener('click', saveEditor);
    panel.querySelector('#acb-editor-cancel').addEventListener('click', () => {
      hideEditor();
      setStatus('Command edit canceled. No data changed.', 'info');
    });

    panel.querySelector('#acb-manage-list').addEventListener('click', event => {
      const button = event.target.closest('button[data-manage]');
      const row = button?.closest('.acb-manage-row');
      if (!button || !row) return;
      const presetId = row.dataset.presetId;
      if (button.dataset.manage === 'edit') showEditor(presetId);
      if (button.dataset.manage === 'up') movePreset(presetId, -1);
      if (button.dataset.manage === 'down') movePreset(presetId, 1);
      if (button.dataset.manage === 'delete') requestDeletePreset(presetId);
    });

    panel.querySelector('#acb-export').addEventListener('click', exportPresets);
    panel.querySelector('#acb-import').addEventListener('click', beginImport);
    panel.querySelector('#acb-confirm-cancel').addEventListener('click', () => {
      clearPendingAction();
      setStatus('Pending destructive action canceled. No data changed.', 'info');
    });
    panel.querySelector('#acb-confirm-run').addEventListener('click', confirmPendingAction);
    panel.querySelector('#acb-dismiss-status').addEventListener('click', () => {
      setStatus('Ready. Append preserves composer content; long ChatGPT prompts use file delivery by default; Auto chain advances Core -> Second -> Performance.', 'info');
    });

    const titlebar = panel.querySelector('#acb-titlebar');

    const finishDrag = (event = null, reason = 'pointer') => {
      if (!drag) return;
      if (event?.pointerId != null && event.pointerId !== drag.pointerId) return;

      if (dragFrame) {
        cancelAnimationFrame(dragFrame);
        dragFrame = 0;
      }

      const pointerId = drag.pointerId;
      drag = null;

      try {
        if (titlebar.hasPointerCapture?.(pointerId)) titlebar.releasePointerCapture(pointerId);
      } catch (_) { }

      if (!commitStateMutation(
        () => { clampPanelPosition({ commit: true }); },
        'Panel position could not be persisted; the previous stored position was restored.'
      )) return;

      if (reason === 'pointer') {
        setStatus('Panel position saved.', 'success');
      } else if (reason === 'viewport') {
        setStatus('Drag ended because the viewport changed. The current visible position was saved safely.', 'info');
      }
    };

    titlebar.addEventListener('pointerdown', event => {
      if (
        state.posLocked ||
        event.button !== 0 ||
        event.isPrimary === false ||
        event.target.closest('button, select, input, textarea, label')
      ) return;

      const visiblePosition = clampPanelPosition();
      if (!visiblePosition) return;

      // Cache the viewport bounds and rendered panel size once at drag start.
      // Pointer-move frames then compute the clamped position from this cached
      // geometry instead of re-running the whole display-layout sync (which
      // rewrites width/height/opacity and forces a layout read) per raw event.
      const viewport = visiblePosition.viewport;
      const marginX = Math.min(PANEL_EDGE_MARGIN, Math.max(0, (viewport.width - visiblePosition.width) / 2));
      const marginY = Math.min(PANEL_EDGE_MARGIN, Math.max(0, (viewport.height - visiblePosition.height) / 2));
      const minX = viewport.left + marginX;
      const minY = viewport.top + marginY;

      drag = {
        pointerId: event.pointerId,
        startX: event.clientX,
        startY: event.clientY,
        originX: visiblePosition.x,
        originY: visiblePosition.y,
        minX,
        minY,
        maxX: Math.max(minX, viewport.right - visiblePosition.width - marginX),
        maxY: Math.max(minY, viewport.bottom - visiblePosition.height - marginY)
      };

      try { titlebar.setPointerCapture(event.pointerId); } catch (_) { }
      event.preventDefault();
    });

    window.addEventListener('pointermove', event => {
      if (!drag || event.pointerId !== drag.pointerId) return;

      state.popupPos.x = drag.originX + (event.clientX - drag.startX);
      state.popupPos.y = drag.originY + (event.clientY - drag.startY);

      // At most one geometry-free position update per animation frame; the
      // latest pointer coordinates win, redundant high-rate moves are dropped.
      if (!dragFrame) {
        dragFrame = requestAnimationFrame(() => {
          dragFrame = 0;
          if (!drag) return;
          const x = clampNumber(state.popupPos.x, drag.minX, drag.maxX);
          const y = clampNumber(state.popupPos.y, drag.minY, drag.maxY);
          panel.style.setProperty('left', `${x}px`, 'important');
          panel.style.setProperty('top', `${y}px`, 'important');
        });
      }

      if (event.cancelable) event.preventDefault();
    }, { passive: false });

    window.addEventListener('pointerup', event => finishDrag(event, 'pointer'));
    window.addEventListener('pointercancel', event => finishDrag(event, 'cancel'));
    titlebar.addEventListener('lostpointercapture', event => finishDrag(event, 'capture-lost'));
    window.addEventListener('blur', () => finishDrag(null, 'blur'));
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) {
        finishDrag(null, 'hidden');
      } else {
        scheduleBridgeFlush(100);
      }
    });

    const syncViewport = () => {
      if (viewportSyncFrame) return;
      viewportSyncFrame = requestAnimationFrame(() => {
        viewportSyncFrame = 0;

        // Resizing/maximizing/restoring during a drag used to leave stale drag
        // coordinates. End that drag first, then clamp only the rendered copy.
        if (drag) finishDrag(null, 'viewport');

        clampPanelPosition({ report: true });
        updateLockState();
      });
    };

    window.addEventListener('resize', syncViewport);
    window.addEventListener('orientationchange', syncViewport);
    window.addEventListener('focus', () => {
      syncViewport();
      scheduleBridgeFlush(100);
    });
    window.addEventListener('pageshow', () => {
      syncViewport();
      scheduleBridgeFlush(100);
    });
    document.addEventListener('fullscreenchange', syncViewport);

    if (window.visualViewport) {
      window.visualViewport.addEventListener('resize', syncViewport);
      window.visualViewport.addEventListener('scroll', syncViewport);
    }
  }

  function clearWidgetBootstrapRetries() {
    for (const timer of widgetBootstrapTimers) clearTimeout(timer);
    widgetBootstrapTimers = [];
    if (widgetBootstrapObserver) {
      widgetBootstrapObserver.disconnect();
      widgetBootstrapObserver = null;
    }
  }

  function ensureWidgetConnected() {
    if (!document.body) return false;

    // React/ChatGPT may replace or temporarily rebuild top-level document
    // children during hard reload/hydration. Keep the already-initialized panel
    // alive instead of creating a second widget and duplicating listeners.
    if (panel) {
      if (!panel.isConnected) document.body.appendChild(panel);
      if (fileInput && !fileInput.isConnected) document.body.appendChild(fileInput);
      return panel.isConnected;
    }

    try {
      mount();
      return Boolean(panel?.isConnected);
    } catch (error) {
      console.error('[AUDAPACK Widget] widget mount failed; bootstrap will retry.', error);
      try { panel?.remove(); } catch (_) { }
      try { fileInput?.remove(); } catch (_) { }
      panel = null;
      fileInput = null;
      return false;
    }
  }

  function installWidgetGuardian() {
    if (typeof MutationObserver !== 'function') return;

    if (!widgetGuardianObserver) {
      widgetGuardianObserver = new MutationObserver(() => {
        const bodyChanged = widgetGuardianBody !== document.body;
        const auditRootDetached = Boolean(autoAuditObserverRoot && !autoAuditObserverRoot.isConnected);
        if (bodyChanged) installWidgetGuardian();
        if (!panel?.isConnected) ensureWidgetConnected();
        if (auditRootDetached) {
          ensureAutoAuditObserver();
          if (autoRuntime?.enabled) scheduleAutoAuditCheck(80);
        }
      });
    }

    widgetGuardianObserver.disconnect();
    widgetGuardianBody = document.body || null;

    // Only watch top-level children. This catches BODY replacement and removal
    // of our direct BODY child without subscribing to ChatGPT's mutation-heavy
    // streaming subtree.
    if (document.documentElement) {
      widgetGuardianObserver.observe(document.documentElement, { childList: true });
    }
    if (document.body) {
      widgetGuardianObserver.observe(document.body, { childList: true });
    }

    // `main` can be replaced without removing BODY itself. Watch only its direct
    // parent, not the whole streaming subtree, so root replacement is detected
    // cheaply and the Auto3 observer can be re-anchored.
    const auditRoot = document.querySelector?.('main');
    const auditRootParent = auditRoot?.parentElement || null;
    if (
      auditRootParent &&
      auditRootParent !== document.body &&
      auditRootParent !== document.documentElement
    ) {
      widgetGuardianObserver.observe(auditRootParent, { childList: true });
    }
  }

  function armWidgetBootstrap() {
    const attempt = () => {
      if (!ensureWidgetConnected()) return false;
      clearWidgetBootstrapRetries();
      installWidgetGuardian();
      return true;
    };

    if (attempt()) return;

    // @run-at document-start is early enough that documentElement/body are not
    // guaranteed to exist in every Brave/Tampermonkey navigation. Observe the
    // Document itself first, then switch to the cheap top-level guardian after
    // a successful mount. This closes the intermittent F5 "no widget" race.
    if (typeof MutationObserver === 'function') {
      widgetBootstrapObserver = new MutationObserver(() => { attempt(); });
      try {
        widgetBootstrapObserver.observe(document, { childList: true, subtree: true });
      } catch (error) {
        console.error('[AUDAPACK Widget] bootstrap observer failed; timed retries remain active.', error);
      }
    }

    widgetBootstrapTimers = WIDGET_BOOTSTRAP_RETRY_DELAYS_MS.map(delay =>
      setTimeout(attempt, delay)
    );

    document.addEventListener('DOMContentLoaded', attempt, { once: true });
    document.addEventListener('readystatechange', attempt);
    window.addEventListener('load', attempt, { once: true });
    window.addEventListener('pageshow', attempt);
    window.addEventListener('focus', attempt);
  }

  function mount() {
    if (panel || !document.body) return;
    state = loadState();
    GM_addStyle(CSS);

    const site = detectSite();
    panel = document.createElement('section');
    panel.id = 'acb-popup';
    panel.setAttribute('role', 'complementary');
    panel.setAttribute('aria-label', 'AUDAPACK Widget');
    setHTML(panel, `
      <div id="acb-titlebar">
        <div id="acb-title">AUDAPACK Widget</div>
        <div id="acb-site" title="Current site">${escapeHTML(site.label)}</div>

        <div id="acb-super-controls" aria-label="Super compact Auto3 monitor">
          <span id="acb-super-brand" title="No audit project detected in this chat.">CHAT</span>
          <label id="acb-super-auto-label" for="acb-super-enabled" title="Auto3 for this chat">
            <span>A3</span>
            <input id="acb-super-enabled" type="checkbox" aria-label="Auto3 for this chat" />
          </label>
          <div id="acb-super-progress" aria-label="Audit stages; completed stages can be clicked to copy">
            <button type="button" class="acb-super-step" data-step="1" data-state="idle" title="Audit Core">1</button>
            <button type="button" class="acb-super-step" data-step="2" data-state="idle" title="Audit Second Wave">2</button>
            <button type="button" class="acb-super-step" data-step="3" data-state="idle" title="Audit Performance">3</button>
          </div>
          <button id="acb-super-state" type="button" data-kind="info" data-action="sync-save" title="Audit state. READY becomes START when a project file is attached; otherwise click to SYNC/SAVE current audit state.">CHAT</button>
        </div>

        <button id="acb-new-chat" type="button" title="New Chat (switches this tab to a fresh new chat without spawning new tabs)">+</button>
        <button id="acb-super-toggle" type="button" aria-pressed="false" title="Switch to the one-line Auto3 monitor">Mini</button>
        <button id="acb-collapse" type="button" aria-expanded="true" title="Collapse the widget to the title bar">Collapse</button>
      </div>

      <div id="acb-tabs" role="tablist" aria-label="AUDAPACK Widget views">
        <button type="button" role="tab" data-view="commands" aria-selected="true">Run</button>
        <button type="button" role="tab" data-view="manage" aria-selected="false">Edit</button>
        <button type="button" role="tab" data-view="settings" aria-selected="false">Settings</button>
      </div>

      <div id="acb-content">
        <div id="acb-view-commands" class="acb-view" role="tabpanel">
          <div id="acb-auto-audit">
            <div id="acb-auto-head">
              <label id="acb-auto-toggle-label" for="acb-auto-enabled" title="Automatically continue Core -> Second Wave -> Performance for this ChatGPT conversation only.">
                <input id="acb-auto-enabled" type="checkbox" />
                <span>Auto3</span>
              </label>
              <button id="acb-auto-adopt" type="button" title="Resume/recover automation from the latest audit turn in this ChatGPT conversation.">Resume</button>
              <button id="acb-auto-stop" type="button" title="Pause the active chain without disabling Auto3.">Pause</button>
            </div>
            <div id="acb-auto-progress" aria-label="Audit chain progress">
              <button type="button" class="acb-auto-step" data-step="1" data-state="idle">1 Core</button>
              <button type="button" class="acb-auto-step" data-step="2" data-state="idle">2 Second</button>
              <button type="button" class="acb-auto-step" data-step="3" data-state="idle">3 Perf</button>
            </div>
            <div id="acb-auto-state-row">
              <div id="acb-auto-state" data-kind="info">Auto chain disabled.</div>
              <button id="acb-save-now" type="button" data-state="idle" title="SYNC/SAVE current chat: persist runtime, rescan COMPLETE waves, force-confirm disk output, and refresh ALL_3 when possible.">SAVE</button>
            </div>
            <div id="acb-audit-copy-hint">Attach project + START = arm Auto3 automatically · normal chat stays inert · every COMPLETE is saved.</div>
          </div>

          <div id="acb-audit-quick">
            <div class="acb-section-title">Audit workflow</div>
            <div id="acb-audit-quick-list" aria-label="Pinned audit waves"></div>
          </div>

          <div id="acb-other-commands" hidden>
            <div class="acb-section-title">Other commands</div>
            <div id="acb-command-tools">
              <div id="acb-catbar" role="tablist" aria-label="Command categories"></div>
              <div id="acb-filter-wrap">
                <input id="acb-filter" type="text" autocomplete="off" aria-label="Filter custom commands" placeholder="Filter commands..." />
              </div>
            </div>
            <div id="acb-command-list" aria-live="off"></div>
          </div>
        </div>

        <div id="acb-view-manage" class="acb-view acb-view-scroll" role="tabpanel" hidden>
          <div class="acb-section">
            <div class="acb-section-title">Categories</div>
            <div class="acb-field">
              <label class="acb-label" for="acb-manage-category">Selected category</label>
              <select id="acb-manage-category"></select>
            </div>
            <div class="acb-field">
              <label class="acb-label" for="acb-category-name">Category name</label>
              <input id="acb-category-name" type="text" maxlength="30" />
            </div>
            <div class="acb-row">
              <button id="acb-add-category" type="button">Add</button>
              <button id="acb-rename-category" type="button">Rename</button>
              <button id="acb-delete-category" type="button">Delete</button>
            </div>
          </div>

          <div class="acb-section">
            <div class="acb-section-title">Commands</div>
            <div id="acb-manage-list"></div>
            <div class="acb-row">
              <button id="acb-add-command" type="button">Add command</button>
            </div>
          </div>

          <div id="acb-editor" class="acb-section" hidden>
            <div id="acb-editor-title" class="acb-section-title">Add command</div>
            <div class="acb-field">
              <label class="acb-label" for="acb-edit-name">Name *</label>
              <input id="acb-edit-name" type="text" maxlength="40" />
            </div>
            <div class="acb-field">
              <label class="acb-label" for="acb-edit-desc">Description</label>
              <input id="acb-edit-desc" type="text" maxlength="100" />
            </div>
            <div class="acb-field">
              <label class="acb-label" for="acb-edit-text">Prompt *</label>
              <textarea id="acb-edit-text"></textarea>
            </div>
            <div id="acb-editor-actions">
              <button id="acb-editor-cancel" type="button">Cancel</button>
              <button id="acb-editor-save" type="button">Save</button>
            </div>
          </div>

          <div class="acb-section">
            <div class="acb-section-title">Confirm action</div>
            <div id="acb-confirm-text">Nothing pending.</div>
            <div class="acb-row">
              <button id="acb-confirm-cancel" type="button" disabled title="No destructive action is pending.">Cancel</button>
              <button id="acb-confirm-run" type="button" disabled title="No destructive action is pending.">Confirm</button>
            </div>
          </div>
        </div>

        <div id="acb-view-settings" class="acb-view acb-view-scroll" role="tabpanel" hidden>
          <div class="acb-section">
            <div class="acb-section-title">Display</div>
            <div id="acb-displaybar" aria-label="Widget display controls">
              <div class="acb-display-field">
                <label for="acb-size">Size</label>
                <select id="acb-size" aria-label="Widget size">
                  <option value="compact">Small</option>
                  <option value="normal">Normal</option>
                  <option value="large">Large</option>
                </select>
              </div>
              <div class="acb-display-field">
                <label for="acb-opacity">Opacity</label>
                <select id="acb-opacity" aria-label="Widget opacity">
                  <option value="100">100%</option>
                  <option value="75">75%</option>
                  <option value="50">50%</option>
                  <option value="25">25%</option>
                </select>
              </div>
              <button id="acb-lock" type="button" aria-pressed="false" title="Lock or unlock panel position">Lock position</button>
            </div>
          </div>

          <div class="acb-section">
            <div class="acb-section-title">Auto 3 waves</div>
            <div class="acb-section-note">Smart mode: normal chat is inert. Attach a project and press START to arm the full Core → Second → Performance chain automatically.</div>
            <div id="acb-auto-config">
              <div class="acb-auto-field">
                <label for="acb-auto-gate">Completion gate</label>
                <select id="acb-auto-gate" title="Strict requires COMPLETE before advancing to the NEXT wave. PARTIAL and silent idle/stopped responses automatically continue the SAME wave until COMPLETE.">
                  <option value="strict">Strict</option>
                  <option value="relaxed">Relaxed</option>
                </select>
              </div>
              <div class="acb-auto-field">
                <label for="acb-auto-delay">Next-wave delay</label>
                <select id="acb-auto-delay">
                  <option value="500">0.5 s</option>
                  <option value="1200">1.2 s</option>
                  <option value="2500">2.5 s</option>
                  <option value="5000">5 s</option>
                  <option value="10000">10 s</option>
                </select>
              </div>
              <div class="acb-auto-field">
                <label for="acb-auto-timeout">Maximum wait</label>
                <select id="acb-auto-timeout">
                  <option value="60">60 min</option>
                  <option value="120">120 min</option>
                  <option value="180">180 min</option>
                  <option value="360">360 min</option>
                </select>
              </div>
              <div class="acb-auto-field">
                <label for="acb-prompt-delivery">ChatGPT delivery</label>
                <select id="acb-prompt-delivery" title="Auto attaches long ChatGPT prompts as Markdown files instead of inserting the full text into ProseMirror.">
                  <option value="auto">Auto file</option>
                  <option value="file">Always file</option>
                  <option value="text">Text only</option>
                </select>
              </div>
            </div>
            <button id="acb-auto-reset" type="button" title="Discard only this conversation's saved automation chain and wait for a fresh Core turn.">Reset saved audit chain</button>
          </div>

          <div class="acb-section">
            <div class="acb-section-title">Audit output automation</div>
            <div class="acb-section-note">Completed waves are cached first. AUDAPACK Bridge is the primary unattended disk route; bridge/disk outages never stop Auto3.</div>

            <div id="acb-audit-output-controls">
              <label class="acb-check-row" for="acb-auto-rename-chat" title="Rename the current ChatGPT conversation to the detected project name.">
                <input id="acb-auto-rename-chat" type="checkbox" />
                <span>Rename chat to project</span>
              </label>
              <div class="acb-section-note" title="Every structurally COMPLETE audit wave is always queued for persistence. This is no longer user-disableable.">Auto SAVE on COMPLETE: ALWAYS ON</div>
            </div>

            <div class="acb-section-title">AUDAPACK Bridge</div>
            <div id="acb-bridge-config">
              <label class="acb-check-row acb-bridge-wide" for="acb-bridge-enabled" title="Use local AUDAPACK Bridge instead of browser folder permissions.">
                <input id="acb-bridge-enabled" type="checkbox" />
                <span>Use AUDAPACK Bridge (recommended)</span>
              </label>

              <div class="acb-field acb-bridge-wide">
                <label class="acb-label" for="acb-bridge-url">Bridge URL</label>
                <input id="acb-bridge-url" type="text" spellcheck="false" autocomplete="off" value="http://127.0.0.1:17843" />
              </div>

              <div class="acb-field acb-bridge-wide">
                <label class="acb-label" for="acb-bridge-token">Token</label>
                <input id="acb-bridge-token" type="password" spellcheck="false" autocomplete="new-password" placeholder="Paste once; stored token is never displayed" />
              </div>

              <button id="acb-bridge-save-token" type="button" title="Store the token in Tampermonkey storage and retry authentication failures.">Save token</button>
              <button id="acb-bridge-check" type="button" title="Check /health and authenticated /v1/status, then flush queued audit saves.">Check + flush</button>
            </div>
            <div id="acb-bridge-state" data-state="warning">UNKNOWN · queued 0 · failed 0</div>

            <div id="acb-browser-fallback">
              <div class="acb-section-note">Optional browser-folder fallback. Used only when AUDAPACK Bridge is disabled.</div>
              <div id="acb-fallback-output-controls">
                <button id="acb-choose-audit-folder" type="button" title="Optional File System Access fallback.">Choose fallback folder</button>
                <button id="acb-flush-audit-files" type="button" title="Write cached results through the browser folder fallback.">Save cached via fallback</button>
              </div>
              <div id="acb-audit-folder-state" data-state="warning">Fallback folder not checked yet.</div>
            </div>
          </div>

          <div class="acb-section">
            <div class="acb-section-title">Preset data</div>
            <div id="acb-settings-data">
              <button id="acb-import" type="button">Import presets</button>
              <button id="acb-export" type="button">Export presets</button>
            </div>
          </div>
        </div>
      </div>

      <div id="acb-status" role="status" aria-live="polite">
        <div id="acb-status-text" data-kind="info">Ready. Run an audit wave or enable Auto 3 waves.</div>
        <button id="acb-dismiss-status" type="button" title="Clear status">×</button>
      </div>
    `);

    fileInput = document.createElement('input');
    fileInput.type = 'file';
    fileInput.accept = '.json,application/json';
    fileInput.hidden = true;
    fileInput.addEventListener('change', handleImportFile);

    document.body.appendChild(panel);
    document.body.appendChild(fileInput);

    renderTabs();
    renderCategoryTabs();
    renderCommands();
    renderManageCategory();
    renderManageList();
    renderConfirm();
    bindAutoRuntimeToCurrentConversation({ claim: false });
    renderAutoAuditState();
    applyDisplayState();
    clampPanelPosition({ report: true });
    updateLockState();
    installBridgeQueueListener();
    installAuditResultListener();

    if (site.key === 'chatgpt') {
      startAutoAuditMonitor({ immediate: true });

      if (state.bridgeEnabled) {
        setTimeout(() => {
          checkBridge().catch(() => { });
          scheduleBridgeFlush(100);
        }, 500);
      } else {
        refreshAuditDirectoryState().catch(() => { });
      }

      setTimeout(() => {
        const captured = backfillVisibleCompletedAuditResults();
        if (captured) {
          for (const record of currentChatAuditRecords()) {
            if (state.autoSaveAuditFiles && state.bridgeEnabled && record?.text && !record.bridgeSavedAt) {
              enqueueBridgeAuditRecord(record);
            }
          }
          renderAutoAuditState();
          if (autoRuntime?.enabled && autoRuntime.stage === 'idle') {
            reconcileEnabledIdleAuditRuntime(getChatGPTTurns(), { source: 'delayed-backfill' });
          }
        }
      }, 1200);
    }

    // Bind page/global UI listeners last. If any earlier bootstrap/runtime step
    // throws, ensureWidgetConnected() can retry without leaving duplicate window
    // pointer/resize/visibility listeners attached to an abandoned panel.
    attachEvents();
  }

  function releasePageAutomationOwnership() {
    disconnectProjectTitleObserver();

    if (manualAuditSyncFeedbackTimer) {
      clearTimeout(manualAuditSyncFeedbackTimer);
      manualAuditSyncFeedbackTimer = 0;
    }

    conversationTitleGuardToken = '';
    conversationTitleGuardStartedAt = 0;
    conversationTitleGuardProject = '';
    conversationTitleGuardConversationKey = '';
    conversationTitleGuardRunStartedAt = 0;

    const pendingStart = readStartAuditHandoff();
    if (pendingStart && !startHandoffIsCommitted(pendingStart) && !startHandoffIsPrepared(pendingStart)) {
      clearStartAuditHandoff();
    }
    const committedSend = readCommittedAutoSend();
    if (committedSend && committedSend.phase !== 'clicked') clearCommittedAutoSend();
    releaseAutoLease(autoBoundConversationKey);
    releaseBridgeFlushLease();
    if (autoBoundConversationKey?.startsWith('c:')) {
      try { sessionStorage.removeItem(AUTO_DRAFT_SESSION_KEY); } catch (_) { }
    }
  }

  window.addEventListener('pagehide', releasePageAutomationOwnership);
  window.addEventListener('beforeunload', releasePageAutomationOwnership);

  function init() {
    armWidgetBootstrap();
  }

  if (globalThis.__ACB_ENABLE_TEST_HOOK__) {
    Object.defineProperty(globalThis, '__ACB_TEST__', {
      configurable: true,
      value: {
        version: STATE_VERSION,
        constants: {
          AUDIT_COMMAND_MARKERS,
          ASSISTANT_AUTHORED_CONTENT_SELECTOR,
          ASSISTANT_RESPONSE_ACTIONS_SELECTOR,
          AUTO_LEASE_PREFIX,
          AUTO_RUNTIME_PREFIX,
          AUTO_LEGACY_RUNTIME_KEY,
          AUTO_LEGACY_SESSION_KEY,
          AUTO_AUDIT_RESULT_SIGNAL_KEY,
          AUTO_TAB_SESSION_KEY,
          AUTO_DRAFT_SESSION_KEY,
          BRIDGE_DEFAULT_URL,
          BRIDGE_JOB_PREFIX,
          BRIDGE_QUEUE_SIGNAL_KEY
        },
        storage: {
          gmGet: GM_getValue,
          gmSet: GM_setValue,
          gmDelete: GM_deleteValue
        },
        get autoRuntime() { return autoRuntime; },
        get autoBoundConversationKey() { return autoBoundConversationKey; },
        get autoInstanceId() { return autoInstanceId; },
        classifyAuditMessage,
        classifyAuditTurn,
        auditTurnIsContinuation,
        knownAuditReceiptKind,
        trivialStartComposerNoise,
        prepareComposerForExplicitAuditStart,
        projectNameFromAuditText,
        looksOpaqueIdentity,
        sanitizeProjectIdentity,
        supportedProjectAttachmentFilename,
        projectNameFromArtifactFilename,
        projectNameFromAttachmentFilename,
        projectNameFromComposerAttachments,
        reconcileProjectIdentityFromComposer,
        currentSidebarConversationTitle,
        sanitizeConversationLabel,
        documentConversationLabel,
        currentMiniIdentity,
        currentConversationTitleMatches,
        waitForVisibleConversationTitle,
        applyLocalSidebarConversationTitle,
        applyPersistentLocalProjectTitle,
        ensureProjectTitleObserver,
        disconnectProjectTitleObserver,
        markConversationTitlePersisted,
        renameGuardContextValid,
        scheduleConversationTitleGuard,
        auditResultFilename,
        captureCompletedAudit,
        readAuditResultFresh,
        writeAuditResult,
        patchAuditResult,
        auditResultWriteMatchesCurrentRuntime,
        currentChatAuditRecords,
        visibleAuditLineage,
        createAuditRunId,
        normalizedBridgeUrl,
        bridgeQueueStats,
        enqueueBridgeAuditRecord,
        bridgeJobRequest,
        readBridgeJob,
        auditHandoffIntegrity,
        concreteHandoffState,
        responseGate,
        auditTestMetadata,
        parseAuditHandoffParts,
        buildCompactAll3Text,
        checkBridge,
        flushBridgeQueue,
        renewBridgeFlushLease,
        flushBridgeQueueManualReliable,
        createBridgeMaterializeReceipt,
        createBridgeMaterializeRunId,
        setManualAuditSyncFeedback,
        saveCurrentChatAuditsNow,
        syncSaveCurrentChatStateNow,
        setAuditAutoSaveEnabled,
        autoProgressSnapshot,
        userTurnTextCandidates,
        latestChatGPTAssistantTurn,
        latestRecognizableAuditUserTurn,
        activeStageAnchorId,
        recoverVirtualizedStageAssistant,
        visibleUserConflictsWithActiveStage,
        resumeAutoAuditFromConversation,
        resetAutoAuditRuntime,
        buildAssistantSnapshot,
        completedAssistantCandidate,
        setAutoAuditEnabled,
        findAssistantRecoveryControl,
        isAuthoredAssistantContent,
        assistantNeedsContinuation,
        assistantHasRetryError,
        assistantContinueGeneratingButton,
        assistantRetryButton,
        verifyAutoLeaseForSend,
        isLeaseTokenCurrent,
        claimAutoLease,
        readAutoLease,
        writeAutoLease,
        releaseAutoLease,
        chatGPTComposerStateSnapshot,
        sameComposerState,
        createAutoSendOwnershipGuard,
        chatGPTComposerReadyForAutoSend,
        chatGPTComposerRoot,
        rawChatGPTComposerInput,
        getChatGPTInput,
        getChatGPTSend,
        isChatGPTSend,
        chatGPTSendNearComposer,
        chatGPTComposerReceiptState,
        chatGPTComposerStillContainsReceipt,
        chatGPTSendAccepted,
        clickChatGPTSendVerified,
        chatGPTComposerAttachmentTiles,
        chatGPTReadyComposerAttachments,
        chatGPTReadyAttachmentSummary,
        composerAttachmentSignature,
        mutationTargetsOwnWidget,
        externalMutationRecords,
        mutationTouchesNode,
        scheduleMiniAttachmentRefresh,
        miniStartAuditState,
        startAuditCoreFromReadyAttachment,
        startHandoffComposerStillPrepared,
        recoverArmedStartSend,
        readStartAuditHandoff,
        beginStartAuditHandoff,
        armStartAuditHandoffForSend,
        markStartAuditHandoffClicking,
        startHandoffIsPrepared,
        startHandoffIsCommitted,
        startHandoffRouteProven,
        markStartAuditHandoffSent,
        runtimeIsBlankDisabled,
        runtimeIsStartClaimable,
        startHandoffCanFollowRoute,
        migrateStartHandoffRuntime,
        recoverSentStartCore,
        chatGPTIsGenerating,
        chatGPTAuthInterstitialVisible,
        chatGPTLoggedOutRootVisible,
        chatGPTRootIsQuarantined,
        rememberStableConversationKey,
        lastStableConversationKey,
        readA3Intent,
        writeA3Intent,
        a3IntentAllowsConversation,
        adoptA3IntentForConversation,
        committedStartOwnsConversationKey,
        forceCommittedStartEnabledForKey,
        activeAuditNeedsRouteProtection,
        shouldPreservePreviousStableKey,
        reconcileEnabledIdleAuditRuntime,
        composerPlainText,
        previousAuditUserTurn,
        auditUserFlowAfter,
        latestExpectedAuditUserTurn,
        latestAssistantAfterTurn,
        assistantStronglyMatchesAuditWave,
        auditAssistantAcrossSupplementals,
        sidecarContextCountAfter,
        watchSupplementalAuditContext,
        latestChatGPTUserTurn,
        armFromCoreTurn,
        resumeRuntimeFromAuditTurn,
        getTurnId,
        turnRole,
        getChatGPTTurns,
        findTurnById,
        stageForAuditKind,
        reconcileExecutionGap,
        loadLegacyAutoRuntimeForCurrentConversation,
        readStoredRuntime,
        loadAutoRuntime,
        normalizeAutoRuntime,
        emptyAutoRuntime,
        persistRuntimeForKey,
        refreshAutoRuntimeFromStorage,
        bindAutoRuntimeToCurrentConversation,
        saveAutoRuntime,
        pauseAutoAudit,
        renderAutoAuditState,
        scheduleAutoAuditCheck,
        clearAutoTimers,
        currentConversationKey,
        detectSite,
        ensureAutoAuditObserver,
        autoAuditObserverConfig,
        ensureWidgetConnected,
        installWidgetGuardian,
        armWidgetBootstrap,
        mount,
        startAutoAuditMonitor,
        stopAutoAuditMonitor,
        triggerSend,
        executePreset,
        sendAutoAuditWave,
        sendAutoAuditContinuation,
        autoClickAssistantRecovery,
        setStatus
      }
    });
  }

  init();
})();
