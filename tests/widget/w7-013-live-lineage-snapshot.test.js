'use strict';

// T-261 (SAIHANDOFF Milestone A/C/J): the live diagnostic that decides WHICH
// layer lost the Core lineage.
//
// The 2026-10-03 production state was: A3 enabled, the canonical Core visibly
// on screen, ChatGPT visibly generating, the real Stop control detected -- and
// the compact cell reading BUSY. superCompactAutoLabel() returning BUSY is
// only reachable when BOTH the live lineage scan and the runtime stage failed
// to name a wave, so "BUSY" is a SYMPTOM with four distinct causes:
//
//   A TURN_DISCOVERY          the Core exists in the page, discovery missed it
//   B TURN_CLASSIFICATION     the turn was found, the recognizer missed it
//   C RUNTIME_ADOPTION        it is recognized and owned; the runtime is passive
//   D START_ROUTE_OWNERSHIP   a committed START cannot prove it owns this /c/<id>
//
// Repairing the wrong layer is worse than not shipping: it burns a release on
// code the live page never exercised. This suite proves the diagnostic can
// tell the four apart, so the next reproduction names its own class.
//
// It also proves the snapshot leaks nothing: no prompt text, no receipt value,
// no tokens. The snapshot is rendered into a tooltip and a clipboard block.

const { test } = require('node:test');
const assert = require('node:assert');
const { setup, mainEl, userTurn, addTurns, runtimeFixture } = require('./helpers');

const RECEIPT = 'startcore-w7-013';
const RUN_ID = 'run-w7-013';
const CONVO = 'c:abc123';
const START_HANDOFF_KEY = 'ai_chatbuttons_auto_start_handoff_v1';
const TAB_SESSION_KEY = 'ai_chatbuttons_auto_tab_id_v1';

// The visible head of a real authored Core: ChatGPT clamps the tail behind
// "Show more", so the canonical header is on screen and the trailing machine
// receipt is not. That is the exact production state, and it is why lineage may
// never depend on re-parsing the prompt.
const CORE_HEAD = [
  'AUDIT CORE — wave 1/3 of Quick 3 Waves.',
  `CAMPAIGN_RUN_ID: ${RUN_ID}`,
  'PROJECT_NAME: TERMINSAI',
  'Instructions follow. '.repeat(400)
].join('\n');

// A real current-build composer with the real current-build Stop control, so
// `generating` is true for the same reason it is true in production: the page
// is not merely talking, generation is on.
function generatingPage(h) {
  const main = mainEl(h);
  const form = h.el('form', { class: 'relative flex flex-col gap-2' });
  const input = h.el('div', {
    contenteditable: 'true',
    role: 'textbox',
    'aria-label': 'Ask ChatGPT',
    class: 'ProseMirror'
  });
  input.isContentEditable = true;
  form.appendChild(input);
  form.appendChild(h.el('button', { type: 'button', 'aria-label': 'Stop' }));
  main.appendChild(form);
  h.mutate(form);
  return { form, input };
}

function enabledRuntime(api, overrides = {}) {
  const runtime = runtimeFixture({ enabled: true, stage: 'idle', conversationKey: CONVO });
  runtime.runId = RUN_ID;
  api.autoRuntime = { ...runtime, ...overrides };
  return api.autoRuntime;
}

// The REAL production sequence: arm at the pre-send hook (where the baseline
// and the transaction identity are frozen), then mark the irreversible click.
function commitRealStartHandoff(api) {
  const handoff = api.beginStartAuditHandoff();
  api.armStartAuditHandoffForSend(handoff);
  return api.markStartAuditHandoffSent(handoff) || api.readStartAuditHandoff();
}

// A committed START belonging to a DIFFERENT conversation: A3 intent is live,
// the durable transaction is real, and it still cannot prove this route.
function commitForeignStart(h) {
  const handoff = {
    version: 1,
    tabId: String(h.sessionStore.get(TAB_SESSION_KEY) || ''),
    sourceKey: 'c:another-chat',
    lastKey: 'c:another-chat',
    phase: 'sent',
    startedAt: Date.now() - 5000,
    armedAt: Date.now() - 4000,
    sentAt: Date.now() - 1000,
    destinationKey: 'c:another-chat',
    receipt: RECEIPT,
    expiresAt: Date.now() + 600000,
    expectedKind: 'core',
    preSendAt: Date.now() - 2000,
    preSendLatestUserTurnId: '',
    preSendUserTurnCount: 0,
    transaction: {
      expectedKind: 'core',
      receipt: RECEIPT,
      preSendUserTurnCount: 0,
      preSendLatestUserTurnId: ''
    }
  };
  h.sessionStore.set(START_HANDOFF_KEY, JSON.stringify(handoff));
  return handoff;
}

// ---------------------------------------------------------------------------
// The reproduction itself
// ---------------------------------------------------------------------------

test('W7-013: the live defect state is reported as a named failure class, never as a clean BUSY', () => {
  const { h, api } = setup();
  enabledRuntime(api);
  generatingPage(h);
  // A user turn the recognizer does not read as an audit command -- the shape a
  // current-build authored turn takes when the semantic content surface moved.
  addTurns(h, [userTurn(h, 'turn-plain', 'what is the weather today?')]);
  h.mutate(mainEl(h));

  const snapshot = api.liveAuditLineageSnapshot();
  assert.strictEqual(snapshot.generationState, 'generating', 'the fixture must really be generating');
  assert.strictEqual(snapshot.liveAuditKind, '', 'no lineage is recognized in this fixture');
  assert.ok(['BUSY', 'ATTN'].includes(snapshot.compactLabel), `compact=${snapshot.compactLabel}`);

  const verdict = api.classifyLiveAuditLineage(snapshot);
  assert.strictEqual(verdict.failure, 'TURN_CLASSIFICATION', verdict.reason);
});

// ---------------------------------------------------------------------------
// One case per class, in the order the classifier decides them
// ---------------------------------------------------------------------------

test('W7-013 CLASS A: an undiscovered Core is TURN_DISCOVERY', () => {
  const { h, api } = setup();
  enabledRuntime(api);
  generatingPage(h);

  const snapshot = api.liveAuditLineageSnapshot();
  assert.strictEqual(snapshot.turnCount, 0, 'no turn is discoverable at all');
  assert.strictEqual(api.classifyLiveAuditLineage(snapshot).failure, 'TURN_DISCOVERY');
});

test('W7-013 CLASS B: a discovered, unrecognized Core is TURN_CLASSIFICATION', () => {
  const { h, api } = setup();
  enabledRuntime(api);
  generatingPage(h);
  addTurns(h, [userTurn(h, 'turn-plain', 'what is the weather today?')]);
  h.mutate(mainEl(h));

  const snapshot = api.liveAuditLineageSnapshot();
  assert.strictEqual(snapshot.userTurnCount, 1, 'the turn WAS discovered');
  assert.strictEqual(snapshot.latestUserTurnDiscovered, true);
  assert.strictEqual(snapshot.latestUserClassifiedKind, '', 'the recognizer missed it');
  assert.strictEqual(api.classifyLiveAuditLineage(snapshot).failure, 'TURN_CLASSIFICATION');
});

test('W7-013 CLASS C: a recognized Core with a passive runtime is RUNTIME_ADOPTION even when the label is already CORE', () => {
  const { h, api } = setup();
  enabledRuntime(api);
  generatingPage(h);
  addTurns(h, [userTurn(h, 'turn-core', CORE_HEAD)]);
  h.mutate(mainEl(h));

  const snapshot = api.liveAuditLineageSnapshot();
  assert.strictEqual(snapshot.latestUserClassifiedKind, 'core', 'lineage is recognized');
  assert.strictEqual(snapshot.liveAuditKind, 'core', 'so the cell is not lying');
  assert.strictEqual(snapshot.runtimeStage, 'idle', 'but the runtime never adopted anything');
  assert.strictEqual(snapshot.startHandoffCommitted, false, 'no START transaction claims it');
  const verdict = api.classifyLiveAuditLineage(snapshot);
  assert.strictEqual(verdict.failure, 'RUNTIME_ADOPTION', verdict.reason);
  assert.strictEqual(verdict.reason, 'recognized-but-runtime-passive');
});

test('W7-013 CLASS D: a committed START that cannot prove the current route is START_ROUTE_OWNERSHIP', () => {
  const { h, api } = setup();
  enabledRuntime(api);
  generatingPage(h);
  commitForeignStart(h);
  addTurns(h, [userTurn(h, 'turn-core', CORE_HEAD)]);
  h.mutate(mainEl(h));

  const snapshot = api.liveAuditLineageSnapshot();
  assert.strictEqual(snapshot.conversationKey, CONVO);
  assert.strictEqual(snapshot.latestUserClassifiedKind, 'core', 'the turn is still recognized');
  assert.strictEqual(snapshot.startHandoffCommitted, true, 'START is still committed');
  assert.strictEqual(snapshot.startHandoffOwnsIntent, true, 'and still owns A3 intent');
  assert.strictEqual(snapshot.startHandoffRouteOwned, false, 'but NOT this route');
  assert.strictEqual(api.classifyLiveAuditLineage(snapshot).failure, 'START_ROUTE_OWNERSHIP');
});

test('W7-013: a healthy lineage is not a failure', () => {
  const { h, api } = setup();
  enabledRuntime(api, { stage: 'wait-core', expectedKind: 'core' });
  generatingPage(h);
  addTurns(h, [userTurn(h, 'turn-core', CORE_HEAD)]);
  h.mutate(mainEl(h));

  const snapshot = api.liveAuditLineageSnapshot();
  assert.strictEqual(api.classifyLiveAuditLineage(snapshot).failure, 'NONE', 'nothing is broken here');
});

// ---------------------------------------------------------------------------
// The snapshot must be safe to paste into a ticket
// ---------------------------------------------------------------------------

test('W7-013: the snapshot reports shape and never content', () => {
  const { h, api } = setup();
  enabledRuntime(api, { pendingSendKind: 'core', pendingSendReceipt: RECEIPT });
  generatingPage(h);
  // START commits FIRST: the baseline is frozen with zero authored turns, and
  // the Core that appears afterwards is exactly the one turn that crossed it.
  const handoff = commitRealStartHandoff(api);
  assert.ok(handoff && handoff.phase === 'sent', 'the START really committed');
  addTurns(h, [userTurn(h, 'turn-core', CORE_HEAD)]);
  h.mutate(mainEl(h));

  const snapshot = api.liveAuditLineageSnapshot();
  const rendered = JSON.stringify(snapshot);

  assert.ok(!rendered.includes(RECEIPT), 'the receipt VALUE must never appear');
  assert.ok(!rendered.includes('ACB_CHAIN_RECEIPT'), 'the receipt marker must never appear');
  assert.ok(!rendered.includes('AUDIT CORE'), 'prompt text must never appear');
  assert.ok(!rendered.includes('PROJECT_NAME'), 'project metadata must never appear');
  assert.ok(!rendered.includes('Instructions follow'), 'body text must never appear');

  // The facts a repair needs ARE present.
  assert.strictEqual(snapshot.runtimePendingSendReceiptPresent, true, 'presence is allowed, value is not');
  assert.strictEqual(snapshot.exactStartReceiptTurnFound, false, 'the clamped tail is honestly not found');
  assert.strictEqual(snapshot.transactionBoundaryCandidateCount, 1, 'one authored turn crossed the baseline');
  assert.strictEqual(snapshot.startHandoffBaselinePresent, true, 'the pre-send baseline was frozen');
  assert.strictEqual(snapshot.startHandoffRouteOwned, true, 'START owns this route');
  assert.ok(snapshot.latestUserTurnStructure.includes('article'), 'structural shape is reported');
});

test('W7-013: the one-line diagnostic names the class and the deciding facts', () => {
  const { h, api } = setup();
  enabledRuntime(api);
  generatingPage(h);
  addTurns(h, [userTurn(h, 'turn-plain', 'what is the weather today?')]);
  h.mutate(mainEl(h));

  const line = api.liveAuditLineageDiagnosticLine();
  assert.ok(line.includes('lineage=TURN_CLASSIFICATION'), line);
  assert.ok(line.includes('generation=generating'), line);
  assert.ok(line.includes('compact='), line);
  assert.ok(!line.includes(RECEIPT), 'still content-free');
});