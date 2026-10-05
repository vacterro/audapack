'use strict';

// T-249: READY must NEVER describe a ChatGPT conversation that is currently
// generating an audit response. The live defect: an enabled runtime that is
// stored `idle` (route hydration / recovery) rendered READY while ChatGPT was
// visibly streaming the audit, because superCompactAutoLabel derived READY from
// stored stage without first applying live-generation truth, and the idle
// observer sat on childList-only `turns` so a characterData-only stream never
// re-armed reconciliation.

const { test } = require('node:test');
const assert = require('node:assert');
const {
  setup, mainEl, userTurn, assistantTurn, addTurns, composerFixture, runtimeFixture
} = require('./helpers');

const CORE_TURN = [
  'AUDIT CORE — wave 1/3 of Quick 3 Waves.',
  'CAMPAIGN_RUN_ID: run-live',
  'ACB_CHAIN_RECEIPT: startcore-live-1'
].join('\n');

const SECOND_TURN = [
  'AUDIT SECOND WAVE',
  'ACB_CHAIN_RECEIPT: startcore-live-2'
].join('\n');

const PERF_TURN = [
  'AUDIT PERFORMANCE / STABILITY / EFFECTIVENESS',
  'ACB_CHAIN_RECEIPT: startcore-live-3'
].join('\n');

const ARCH_TURN = [
  'AUDIT ARCHITECTURE / SYSTEM INVARIANTS',
  'The complete command is attached as "AUDIT_ARCHITECTURE_176jhkn.md".',
  'Treat that attached file as my full instruction for this turn and execute it exactly;'
    + ' do not merely summarize the file.',
  'ACB_CHAIN_RECEIPT: startcore-live-arch'
].join('\n');

function idleEnabled(h, api, profile = 'quick3') {
  api.state.auditProfile = profile;
  api.autoRuntime = runtimeFixture({ stage: 'idle', enabled: true, profileId: profile });
}

// --- RED CONTROL -----------------------------------------------------------

test('W7-003 RED: enabled idle + visible Stop generating is never READY', () => {
  const { h, api } = setup();
  const { stop } = composerFixture(h);
  idleEnabled(h, api);
  stop.hidden = false; // ChatGPT is streaming

  assert.strictEqual(api.chatGPTIsGenerating(), true, 'the Stop control is visible');
  assert.notStrictEqual(api.superCompactAutoLabel(), 'READY',
    'READY must never describe a generating ChatGPT conversation');
});

// --- Milestone C: compact label precedence ---------------------------------

test('W7-003: idle + generating + canonical Core => CORE', () => {
  const { h, api } = setup();
  const { stop } = composerFixture(h);
  idleEnabled(h, api);
  addTurns(h, [userTurn(h, 'u1', CORE_TURN), assistantTurn(h, 'a1')]);
  stop.hidden = false;
  assert.strictEqual(api.superCompactAutoLabel(), 'CORE');
});

test('W7-003: idle + generating + Second Wave => W2', () => {
  const { h, api } = setup();
  const { stop } = composerFixture(h);
  idleEnabled(h, api);
  addTurns(h, [
    userTurn(h, 'u1', CORE_TURN), assistantTurn(h, 'a1'),
    userTurn(h, 'u2', SECOND_TURN), assistantTurn(h, 'a2')
  ]);
  stop.hidden = false;
  assert.strictEqual(api.superCompactAutoLabel(), 'W2');
});

test('W7-003: idle + generating + Performance => PERF', () => {
  const { h, api } = setup();
  const { stop } = composerFixture(h);
  idleEnabled(h, api);
  addTurns(h, [
    userTurn(h, 'u1', CORE_TURN), assistantTurn(h, 'a1'),
    userTurn(h, 'u2', SECOND_TURN), assistantTurn(h, 'a2'),
    userTurn(h, 'u3', PERF_TURN), assistantTurn(h, 'a3')
  ]);
  stop.hidden = false;
  assert.strictEqual(api.superCompactAutoLabel(), 'PERF');
});

test('W7-003: idle + generating + Super10 wave => profile-aware token, not READY', () => {
  const { h, api } = setup();
  const { stop } = composerFixture(h);
  idleEnabled(h, api, 'super10');
  addTurns(h, [userTurn(h, 'u1', ARCH_TURN), assistantTurn(h, 'a1')]);
  stop.hidden = false;
  const label = api.superCompactAutoLabel();
  assert.notStrictEqual(label, 'READY');
  assert.strictEqual(label, 'W1', 'architecture is wave 1 of super10');
});

test('W7-003: idle + ordinary non-audit generation => not READY, not audit-adopted', () => {
  const { h, api } = setup();
  const { stop } = composerFixture(h);
  idleEnabled(h, api);
  addTurns(h, [userTurn(h, 'u1', 'just a normal question about cats'), assistantTurn(h, 'a1')]);
  stop.hidden = false;

  assert.strictEqual(api.superCompactAutoLabel(), 'BUSY');
  api.reconcileEnabledIdleAuditRuntime(api.getChatGPTTurns());
  assert.strictEqual(api.autoRuntime.stage, 'idle', 'ordinary generation is never adopted as an audit');
});

test('W7-003: idle + not generating + no audit stays READY (readiness contract preserved)', () => {
  const { h, api } = setup();
  composerFixture(h);
  idleEnabled(h, api);
  assert.strictEqual(api.superCompactAutoLabel(), 'READY');
});

// --- Milestone F: attachment must not mask a live audit --------------------

test('W7-003: active audit + attached ZIP tile => audit label wins over READY', () => {
  const { h, api } = setup();
  const { form, stop } = composerFixture(h);
  idleEnabled(h, api);
  addTurns(h, [userTurn(h, 'u1', CORE_TURN), assistantTurn(h, 'a1')]);
  const tile = h.el('div', { role: 'group', 'aria-label': '_SAICONT_27.08.26-T06-28-02.zip' });
  tile.appendChild(h.el('button', { name: 'expand-file-tile', 'aria-label': 'Expand' }));
  form.appendChild(tile);
  stop.hidden = false;
  assert.strictEqual(api.superCompactAutoLabel(), 'CORE');
});

// --- Milestone B: generation detector topology -----------------------------

test('W7-003: an unrelated Stop control outside the composer is not generation', () => {
  const { h, api } = setup();
  composerFixture(h);
  idleEnabled(h, api);
  // A Stop button living elsewhere on the page (e.g. an audio player), nowhere
  // near the composer.
  const stray = h.el('button', { 'aria-label': 'Stop' });
  h.dom.body.appendChild(stray);
  assert.strictEqual(api.chatGPTIsGenerating(), false);
});

test('W7-003: a composer-adjacent Stop control outside the form is generation', () => {
  const { h, api } = setup();
  const { form } = composerFixture(h);
  idleEnabled(h, api);
  // Current ChatGPT can render composer controls in a sibling shell outside the
  // <form>, sharing a near ancestor with the prompt editor.
  const shell = form.parentNode;
  const sibling = h.el('div', {});
  const stop = h.el('button', { 'data-testid': 'stop-button' });
  sibling.appendChild(stop);
  shell.appendChild(sibling);
  assert.strictEqual(api.chatGPTIsGenerating(), true);
});

// --- Milestone D/E: idle reconciliation + observer escalation --------------

test('W7-003: idle observer escalates to stream while a live audit is in flight', () => {
  const { h, api } = setup();
  const { stop } = composerFixture(h);
  idleEnabled(h, api);
  assert.strictEqual(api.autoAuditObserverConfig(), 'turns', 'genuinely idle chat stays cheap');

  addTurns(h, [userTurn(h, 'u1', CORE_TURN), assistantTurn(h, 'a1')]);
  stop.hidden = false;
  assert.strictEqual(api.autoAuditObserverConfig(), 'stream',
    'an enabled idle runtime with a live audit must observe streaming');
});

test('W7-003: idle + ordinary generation opens a bounded look-window but never adopts', () => {
  // T-249 residual seam: enabled+idle+generating opens a BOUNDED characterData
  // recovery window even before lineage is recognizable, so a purely
  // characterData hydration of an audit turn re-arms reconciliation. Ordinary
  // generation rides that same window harmlessly -- it is only permission to keep
  // looking briefly, never an adoption, and the label stays BUSY (never READY,
  // never a wave). The window collapses back to `turns` the instant generation
  // ends (covered in w7-004).
  const { h, api } = setup();
  const { stop } = composerFixture(h);
  idleEnabled(h, api);
  addTurns(h, [userTurn(h, 'u1', 'ordinary question'), assistantTurn(h, 'a1')]);
  stop.hidden = false;
  assert.strictEqual(api.autoAuditObserverConfig(), 'stream',
    'generation is permission to keep looking briefly for a hydrating audit turn');
  assert.strictEqual(api.superCompactAutoLabel(), 'BUSY',
    'ordinary generation shows BUSY, never READY and never a wave token');
  api.reconcileEnabledIdleAuditRuntime(api.getChatGPTTurns());
  assert.strictEqual(api.autoRuntime.stage, 'idle',
    'ordinary generation is never adopted as an audit');
  stop.hidden = true;
  assert.strictEqual(api.autoAuditObserverConfig(), 'turns',
    'once generation ends with no audit lineage the observer returns to cheap turns');
});

test('W7-003: enabled idle reconciler adopts a live Core audit into wait-core', () => {
  const { h, api } = setup();
  const { stop } = composerFixture(h);
  idleEnabled(h, api);
  const user = userTurn(h, 'u1', CORE_TURN);
  const assistant = assistantTurn(h, 'a1');
  assistant._text = 'CORE-001 partial...';
  addTurns(h, [user, assistant]);
  stop.hidden = false;

  assert.strictEqual(api.reconcileEnabledIdleAuditRuntime(api.getChatGPTTurns()), true);
  assert.strictEqual(api.autoRuntime.stage, 'wait-core');
});

test('W7-003: generation ending with no audit lineage never adopts a runtime', () => {
  const { h, api } = setup();
  const { stop } = composerFixture(h);
  idleEnabled(h, api);
  addTurns(h, [userTurn(h, 'u1', 'ordinary'), assistantTurn(h, 'a1')]);
  stop.hidden = true; // generation ended
  assert.strictEqual(api.reconcileEnabledIdleAuditRuntime(api.getChatGPTTurns()), false);
  assert.strictEqual(api.autoRuntime.stage, 'idle');
});
