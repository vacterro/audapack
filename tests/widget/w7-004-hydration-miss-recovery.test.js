'use strict';

// T-249 (residual seam): the guaranteed SECOND reconciliation opportunity.
//
// autoAuditObserverConfig() escalated an enabled+idle runtime to `stream`
// ONLY when liveAuditActivitySnapshot().active -- and `active` needs a
// classifiable audit user turn. During ChatGPT route hydration the audit user
// turn node already exists but its authored text is not yet complete enough for
// classifyAuditTurn(), so:
//   - the observer sits on childList-only `turns`;
//   - the audit turn finishes hydrating / the answer streams as characterData;
//   - the childList-only observer receives NO event;
//   - reconcileEnabledIdleAuditRuntime() never runs again.
//
// The fix: enabled + idle + ChatGPT generating opens a BOUNDED characterData
// window even before lineage is recognizable, so a characterData-only hydration
// re-arms canonical reconciliation. Ordinary non-audit generation rides the same
// window harmlessly (reconcile refuses it) and it collapses back to cheap `turns`
// once generation ends.

const { test } = require('node:test');
const assert = require('node:assert');
const {
  setup, userTurn, assistantTurn, addTurns, composerFixture, runtimeFixture
} = require('./helpers');

const CORE_TURN = [
  'AUDIT CORE — wave 1/3 of Quick 3 Waves.',
  'CAMPAIGN_RUN_ID: run-hydrate',
  'ACB_CHAIN_RECEIPT: startcore-hydrate-1'
].join('\n');

const ARCH_TURN = [
  'AUDIT ARCHITECTURE / SYSTEM INVARIANTS',
  'The complete command is attached as "AUDIT_ARCHITECTURE_176jhkn.md".',
  'Treat that attached file as my full instruction for this turn and execute it exactly;'
    + ' do not merely summarize the file.',
  'ACB_CHAIN_RECEIPT: startcore-hydrate-arch'
].join('\n');

// Not yet an audit command: a leading fragment ChatGPT has hydrated so far.
const UNCLASSIFIABLE = 'AUD';

function idleEnabled(api, profile = 'quick3') {
  api.state.auditProfile = profile;
  api.autoRuntime = runtimeFixture({ stage: 'idle', enabled: true, profileId: profile });
}

// Deliver a characterData-only mutation on the SAME existing turn node, exactly
// as a real MutationObserver would: a subtree characterData mutation reaches the
// observer ONLY when it actually subscribed to characterData. A childList-only
// observer receives nothing -- which is the whole defect. Never a childList
// insertion, never a new node.
function hydrateViaCharacterData(h, api, node) {
  const observer = api.autoAuditObserver;
  assert.ok(observer, 'the auto-audit observer must be bound');
  if (!observer.connected || !observer.options?.characterData) {
    // Faithful to the browser: no characterData subscription => no callback.
    return false;
  }
  observer.callback([{ type: 'characterData', target: node }], observer);
  return true;
}

// --- RED CONTROL 1 — HYDRATION MISS ----------------------------------------

test('W7-004 RED1: characterData-only hydration of an existing turn re-arms reconciliation', async () => {
  const { h, api } = setup();
  const { stop } = composerFixture(h);
  idleEnabled(api);
  api.startAutoAuditMonitor();

  // An audit user turn node already exists, but its authored text is not yet
  // classifiable, and ChatGPT is already generating.
  const user = userTurn(h, 'u1', UNCLASSIFIABLE);
  const assistant = assistantTurn(h, 'a1');
  assistant._text = 'thinking…';
  addTurns(h, [user, assistant]);
  stop.hidden = false;

  // The childList insertion that mounted the turn re-anchors the observer; that
  // is the realistic point at which enabled+idle+generating must open the
  // bounded characterData window.
  api.ensureAutoAuditObserver();

  // Initial truth: not a wave yet, reconciliation refuses.
  assert.strictEqual(api.classifyAuditTurn(user), '', 'initial text is not a classifiable audit wave');
  assert.strictEqual(api.reconcileEnabledIdleAuditRuntime(api.getChatGPTTurns()), false,
    'nothing to reconcile while the turn is unclassifiable');
  assert.strictEqual(api.autoRuntime.stage, 'idle');

  // The observer must actually be armed for characterData; otherwise the stream
  // that arrives only as characterData is invisible.
  assert.strictEqual(api.autoAuditObserverConfig(), 'stream',
    'enabled+idle+generating must open a bounded characterData recovery window');
  assert.strictEqual(api.autoAuditObserver.options.characterData, true,
    'the bound observer must track characterData during the recovery window');

  // Hydrate the SAME node so its content becomes a canonical AUDIT CORE command,
  // then deliver ONLY a characterData mutation (no childList, no new node).
  user._text = CORE_TURN;
  hydrateViaCharacterData(h, api, user);
  await h.settle();

  // Bounded second chance, taken automatically -- no manual Resume, no click.
  assert.strictEqual(api.autoRuntime.stage, 'wait-core',
    'the characterData-only hydration must hand the reconciler its second chance');
});

// --- RED CONTROL 2 — STREAMING-ONLY SECOND CHANCE --------------------------

test('W7-004 RED2 (Core): early evaluate misses, characterData-only stream recovers wait-core', async () => {
  const { h, api } = setup();
  const { stop } = composerFixture(h);
  idleEnabled(api);
  api.startAutoAuditMonitor();

  const user = userTurn(h, 'u1', UNCLASSIFIABLE);
  const assistant = assistantTurn(h, 'a1');
  assistant._text = 'partial…';
  addTurns(h, [user, assistant]);
  stop.hidden = false;
  api.ensureAutoAuditObserver();

  // 1) First evaluate runs too early.
  await api.evaluateAutoAudit();
  await h.settle();
  assert.strictEqual(api.autoRuntime.stage, 'idle', 'runtime stays idle: nothing was classifiable yet');

  // 2/3/4) The node stays mounted; only characterData mutations occur; lineage
  // becomes recognizable.
  user._text = CORE_TURN;
  hydrateViaCharacterData(h, api, user);
  await h.settle();

  // 5/6) Reconciliation runs again and adopts the right wait stage.
  assert.strictEqual(api.autoRuntime.stage, 'wait-core');
});

test('W7-004 RED2 (Super10 architecture): non-Core wave recovers via characterData-only stream', async () => {
  const { h, api } = setup();
  const { stop } = composerFixture(h);
  idleEnabled(api, 'super10');
  api.startAutoAuditMonitor();

  const user = userTurn(h, 'u1', UNCLASSIFIABLE);
  const assistant = assistantTurn(h, 'a1');
  assistant._text = 'partial…';
  addTurns(h, [user, assistant]);
  stop.hidden = false;
  api.ensureAutoAuditObserver();

  await api.evaluateAutoAudit();
  await h.settle();
  assert.strictEqual(api.autoRuntime.stage, 'idle');

  user._text = ARCH_TURN;
  hydrateViaCharacterData(h, api, user);
  await h.settle();

  assert.strictEqual(api.autoRuntime.stage, 'wait-architecture',
    'architecture is wave 1 of super10 and must be adopted, not a hardcoded core');
});

// --- SAFETY — ordinary generation is a bounded look, never an adoption -------

test('W7-004: ordinary non-audit generation never adopts a runtime and collapses back to turns', async () => {
  const { h, api } = setup();
  const { stop } = composerFixture(h);
  idleEnabled(api);
  api.startAutoAuditMonitor();

  const user = userTurn(h, 'u1', 'just an ordinary question about cats');
  const assistant = assistantTurn(h, 'a1');
  assistant._text = 'cats are great';
  addTurns(h, [user, assistant]);
  stop.hidden = false;
  api.ensureAutoAuditObserver();

  // Permission to keep looking, briefly: streaming is armed while generating.
  assert.strictEqual(api.autoAuditObserverConfig(), 'stream');

  // But characterData churn on ordinary generation must never be adopted.
  hydrateViaCharacterData(h, api, user);
  await h.settle();
  assert.strictEqual(api.autoRuntime.stage, 'idle', 'ordinary generation is never adopted as an audit');
  assert.strictEqual(api.superCompactAutoLabel(), 'BUSY', 'ordinary generation shows BUSY, not READY and not a wave');

  // Generation ends with no audit lineage -> return to the cheap observer.
  stop.hidden = true;
  assert.strictEqual(api.autoAuditObserverConfig(), 'turns',
    'once generation ends with no audit lineage the observer returns to cheap turns');
});

test('W7-004: a genuinely idle chat (not generating) never arms characterData', () => {
  const { h, api } = setup();
  composerFixture(h);
  idleEnabled(api);
  api.startAutoAuditMonitor();
  assert.strictEqual(api.autoAuditObserverConfig(), 'turns',
    'no generation, no audit -> no characterData observation');
});
