'use strict';

// T-257: generation truth is part of the AUDIT STATE MACHINE, not a cosmetic
// activity light.
//
// The live 0.0.84 stall: an enabled A3 runtime sat at wait-core with a visibly
// FINISHED ChatGPT response (response actions mounted, no real generation Stop,
// "Worked for 13m 50s" on screen) and the widget still reported BUSY forever,
// with the campaign stuck at 0/3. The cause was not the label:
//
//   - CHATGPT_STOP_SELECTOR carried `button[data-testid*="stop" i]`, so ANY
//     stop-shaped control matched;
//   - chatGPTIsGenerating() admitted that match on chatGPTSendNearComposer(),
//     whose 7-level ancestor walk above the prompt editor accepts essentially
//     any visible element in the bottom page shell.
//
// A false positive therefore short-circuited evaluateAutoAudit() BEFORE
// completedAssistantCandidate(): no SAVE, no wave commit, no next wave.
//
// This file encodes the truth contract: exact generation identity AND proven
// composer ownership, terminal reconciliation against final response actions,
// a bounded stabilization window for the genuine Chrome transition, and an
// explicit ATTN instead of an eternal BUSY when truth stays contradictory.

const { test } = require('node:test');
const assert = require('node:assert');
const {
  setup, mainEl, userTurn, assistantTurn, addTurns, composerFixture, installAcceptedSend
} = require('./helpers');

const RUN_ID = 'run-gen-truth';
const CONVO = 'c:abc123';

const CORE_TURN = [
  'AUDIT CORE — wave 1/3 of Quick 3 Waves.',
  `CAMPAIGN_RUN_ID: ${RUN_ID}`,
  'ACB_CHAIN_RECEIPT: startcore-gen-1'
].join('\n');

function handoffFor(api, waveDef, profileId) {
  const profile = api.EMBEDDED_AUDIT_PROFILES.profiles[profileId];
  const pfx = waveDef.ticket_prefix.replace(/-$/, '');
  const termKey = waveDef.terminal_status_key || waveDef.slug;
  const fields = (waveDef.ticket_fields && waveDef.ticket_fields.length)
    ? waveDef.ticket_fields
    : ['EVIDENCE', 'DEFECT', 'REPAIR', 'VERIFY'];
  return `PROJECT_NAME: AUDAPACK
DATE_TIME: 2026-09-29T16:00:00+03:00
CAMPAIGN_PROFILE: ${profileId}
CAMPAIGN_RUN_ID: ${RUN_ID}
WAVE_ID: ${waveDef.id}
WAVE_INDEX: ${waveDef.ordinal}
WAVE_COUNT: ${profile.waves.length}
WAVE: ${waveDef.wave_header}
STATUS: ${termKey}: COMPLETE
TICKETS: 1
HANDOFF: IMPLEMENTATION_AGENT

[P1] [${pfx}-001] Sample defect issue title
${fields.map(field => `${field}: sample ${field.toLowerCase()}.`).join('\n')}

${waveDef.done_marker.replace(/:\s*$/, '')}: All tickets and handoffs are verified.`;
}

// ChatGPT's own final response-action chrome. Its presence on the latest
// assistant turn is strong evidence the answer is terminal.
function finalActionsChrome(h, turn) {
  const actions = h.el('div', { 'aria-label': 'Response actions' });
  actions.appendChild(h.el('button', {
    'data-testid': 'copy-turn-action-button',
    'aria-label': 'Copy response'
  }));
  turn.appendChild(actions);
  return actions;
}

function withFinalActions(h, turn) {
  finalActionsChrome(h, turn);
  return turn;
}

// The real shape of a finished answer: authored body text plus ChatGPT's own
// response-action chrome as a sibling. Text lives in a child node, so the turn
// is read exactly as the browser delivers it.
function completedAssistant(h, id, text) {
  const turn = assistantTurn(h, id);
  const body = h.el('div', { 'data-message-content-part-type': 'text' });
  body._text = String(text || '');
  turn.appendChild(body);
  finalActionsChrome(h, turn);
  return turn;
}

// The composer's own action shell, just outside the <form> -- the real
// topology getChatGPTSend()/chatGPTStopNearComposer() both have to survive.
function composerShellControl(h, fixture, attrs) {
  const shell = h.el('div', {});
  const control = h.el('button', attrs);
  shell.appendChild(control);
  fixture.form.parentNode.appendChild(shell);
  return control;
}

function campaignWindow(profileId = 'quick3') {
  const { h, api } = setup();
  const fixture = composerFixture(h);
  api.state.auditProfile = profileId;
  api.state.chatgptPromptDelivery = 'text';
  api.autoRuntime = api.emptyAutoRuntime({ enabled: true, profileId });
  api.autoRuntime.conversationKey = CONVO;
  api.autoRuntime.runId = RUN_ID;
  api.autoRuntime.startedAt = Date.now();
  return { h, api, fixture };
}

async function pump(h, budgetMs = 5000) {
  let left = Number(budgetMs);
  for (let guard = 0; guard < 400 && left > 0; guard += 1) {
    await new Promise(resolve => setImmediate(resolve));
    const pending = h.timers.pending().filter(due => due <= left);
    if (!pending.length) break;
    const step = Math.max(...pending);
    h.advance(step);
    left -= step;
  }
  await new Promise(resolve => setImmediate(resolve));
}

// ===========================================================================
// RED CONTROL -- the exact production symptom (Milestone G)
// ===========================================================================

test('W7-008 RED: completed Core + composer-near non-generation stop candidate is not generating', async () => {
  const { h, api, fixture } = campaignWindow();
  const coreDef = api.findWaveDefinitionForStageOrKind('core');

  const user = userTurn(h, 'u-core', CORE_TURN);
  const assistant = completedAssistant(h, 'a-core', handoffFor(api, coreDef, 'quick3'));
  addTurns(h, [user, assistant]);

  // A visible control IN the composer's own action shell whose data-testid
  // contains "stop" but which is not ChatGPT's generation Stop.
  const falseStop = composerShellControl(h, fixture, {
    'data-testid': 'stop-listening',
    'aria-label': 'Stop listening'
  });

  api.autoRuntime.stage = 'wait-core';
  api.setWaveUserId('core', 'u-core');
  assert.strictEqual(api.saveAutoRuntime({ pauseOnFailure: false }), true);

  // 0.0.84 read this fixture as BUSY forever and never evaluated the answer.
  assert.strictEqual(api.chatGPTIsGenerating(), false,
    'a stop-shaped control that is not the generation Stop must never claim generation');
  assert.strictEqual(api.superCompactAutoLabel(), 'CORE',
    'the live wave token must describe the wave, not a phantom BUSY');

  // The finished answer must reach the canonical completion gate, not a stall.
  // First pass only checkpoints the response fingerprint -- the bounded
  // stabilization window is what stops a single frame committing a wave.
  await api.evaluateAutoAudit();
  assert.strictEqual(api.autoRuntime.stage, 'wait-core',
    'the first evaluation may only checkpoint stability');

  // The live symptom was an answer that had been on screen, unchanged, for
  // minutes ("Worked for 13m 50s"). Model exactly that settled window, through
  // the durable runtime the evaluator actually re-reads.
  api.autoRuntime.stableSince = Date.now() - (2 * 60 * 1000);
  assert.strictEqual(api.saveAutoRuntime({ pauseOnFailure: false }), true);
  await api.evaluateAutoAudit();
  assert.notStrictEqual(api.autoRuntime.stage, 'wait-core',
    'the completed Core must leave wait-core');

  // Campaign progression reaches the next-wave path: 0/3 must not persist, and
  // the second wave is sent exactly once. ChatGPT consumes the submitted
  // payload and adds the user turn -- a click alone is never Send acceptance.
  installAcceptedSend(h, fixture);
  await pump(h);

  assert.notStrictEqual(api.autoRuntime.stage, 'wait-core',
    'the completed Core must leave wait-core for good');
  const secondWaveTurns = mainEl(h)
    .querySelectorAll('[data-message-author-role="user"]')
    .filter(turn => /AUDIT SECOND WAVE/.test(String(turn._text || '')));
  assert.strictEqual(secondWaveTurns.length, 1,
    `Wave 2 must be sent exactly once, saw ${secondWaveTurns.length}`);
  assert.notStrictEqual(api.superCompactAutoLabel(), '0/3',
    'Quick3 must not remain at 0/3 after a committed Core');
  assert.ok(falseStop, 'the false stop candidate stays in the DOM throughout');
});

// ===========================================================================
// Milestone B/C/L: identity AND ownership, never either alone
// ===========================================================================

test('W7-008: exact canonical Stop inside the composer is generation', () => {
  const { api, fixture } = campaignWindow();
  fixture.stop.hidden = false;
  assert.strictEqual(api.chatGPTIsGenerating(), true);
  assert.strictEqual(api.chatGPTGenerationSnapshot().selector_class, 'canonical-stop-generating');
});

test('W7-008: composer-adjacent canonical Stop in the action shell is generation', () => {
  const { h, api, fixture } = campaignWindow();
  composerShellControl(h, fixture, { 'data-testid': 'stop-button' });
  assert.strictEqual(api.chatGPTIsGenerating(), true,
    'a sibling action shell carrying the composer Send is proven composer surface');
});

test('W7-008: an unrelated exact Stop elsewhere on the page is not generation', () => {
  const { h, api, fixture } = campaignWindow();
  const stray = h.el('button', { 'aria-label': 'Stop generating' });
  h.dom.body.appendChild(stray);
  const snap = api.chatGPTGenerationSnapshot();
  assert.strictEqual(api.chatGPTIsGenerating(), false,
    'identity alone is not ownership: a page Stop is never generation');
  assert.strictEqual(snap.inside_composer_root, false);
  assert.strictEqual(snap.reason, 'stale_stop_candidate_ignored',
    'the rejected candidate is named in the evidence');
});

test('W7-008: a data-testid containing "stop" that is not generation is not generation', () => {
  const { h, api, fixture } = campaignWindow();
  composerShellControl(h, fixture, { 'data-testid': 'stop-listening' });
  assert.strictEqual(api.chatGPTIsGenerating(), false,
    'the former wildcard admitted this control and stalled live A3');
});

test('W7-008: a voice control with a stop-like identity is not generation', () => {
  const { h, api, fixture } = campaignWindow();
  const voice = h.el('div', { 'data-testid': 'voice-mode-panel' });
  voice.appendChild(h.el('button', { 'data-testid': 'stop-recording', 'aria-label': 'Stop recording' }));
  fixture.form.parentNode.appendChild(voice);
  assert.strictEqual(api.chatGPTIsGenerating(), false,
    'voice/audio controls are never model generation');
});

test('W7-008: response-action controls are never generation', () => {
  const { h, api, fixture } = campaignWindow();
  const actions = h.el('div', { 'data-testid': 'response-actions' });
  actions.appendChild(h.el('button', { 'data-testid': 'stop-share', 'aria-label': 'Stop sharing' }));
  fixture.form.parentNode.appendChild(actions);
  assert.strictEqual(api.chatGPTIsGenerating(), false);
});

// ===========================================================================
// Milestone D: terminal response truth
// ===========================================================================

test('W7-008: a completed assistant with final actions is not generating', () => {
  const { h, api } = campaignWindow();
  addTurns(h, [userTurn(h, 'u1', CORE_TURN), withFinalActions(h, assistantTurn(h, 'a1'))]);
  const snap = api.chatGPTGenerationSnapshot();
  assert.strictEqual(api.chatGPTIsGenerating(), false);
  assert.strictEqual(snap.latest_assistant_has_final_actions, true);
  assert.strictEqual(snap.state, 'idle');
});

test('W7-008: completed assistant + stale stop-like candidate is terminal, not BUSY', () => {
  const { h, api, fixture } = campaignWindow();
  addTurns(h, [userTurn(h, 'u1', CORE_TURN), withFinalActions(h, assistantTurn(h, 'a1'))]);
  composerShellControl(h, fixture, { 'data-testid': 'stop-listening' });
  const snap = api.chatGPTGenerationSnapshot();
  assert.strictEqual(snap.generating, false);
  assert.strictEqual(snap.state, 'terminal');
  assert.strictEqual(snap.diagnostic, 'stale_stop_candidate_ignored',
    'the rejected candidate is named in the evidence, never obeyed');
  assert.strictEqual(snap.selector_class, 'stop-like');
});

// ===========================================================================
// Milestone E/N: bounded stabilization, then an honest ATTN
// ===========================================================================

test('W7-008: canonical Stop + final actions overlap is stabilizing, never an instant commit', () => {
  const { h, api, fixture } = campaignWindow();
  addTurns(h, [userTurn(h, 'u1', CORE_TURN), withFinalActions(h, assistantTurn(h, 'a1'))]);
  fixture.stop.hidden = false;
  const snap = api.chatGPTGenerationSnapshot();
  assert.strictEqual(snap.state, 'stabilizing');
  assert.strictEqual(snap.generating, true,
    'a single frame of overlapping chrome must not commit a wave');
  assert.strictEqual(snap.conflict, false);
});

test('W7-008: an overlap that resolves to terminal returns to normal completion', () => {
  const { h, api, fixture } = campaignWindow();
  addTurns(h, [userTurn(h, 'u1', CORE_TURN), withFinalActions(h, assistantTurn(h, 'a1'))]);
  fixture.stop.hidden = false;
  assert.strictEqual(api.chatGPTGenerationSnapshot().state, 'stabilizing');

  fixture.stop.hidden = true; // ChatGPT finished removing its Stop
  const snap = api.chatGPTGenerationSnapshot();
  assert.strictEqual(snap.generating, false);
  assert.strictEqual(snap.conflict, false);
  assert.strictEqual(snap.state, 'idle');
});

test('W7-008: an overlap that resolves to generation stays active', () => {
  const { h, api, fixture } = campaignWindow();
  const answer = completedAssistant(h, 'a1', 'partial answer');
  addTurns(h, [userTurn(h, 'u1', CORE_TURN), answer]);
  fixture.stop.hidden = false;
  assert.strictEqual(api.chatGPTGenerationSnapshot().state, 'stabilizing');

  answer.querySelector('[aria-label="Response actions"]').remove();
  const snap = api.chatGPTGenerationSnapshot();
  assert.strictEqual(snap.state, 'generating');
  assert.strictEqual(snap.generating, true,
    'a surviving canonical Stop is generation even after the chrome retracted');
});

test('W7-008: a contradiction past the bounded window is ATTN, never eternal BUSY', () => {
  const { h, api, fixture } = campaignWindow();
  addTurns(h, [userTurn(h, 'u1', CORE_TURN), withFinalActions(h, assistantTurn(h, 'a1'))]);
  fixture.stop.hidden = false;
  assert.strictEqual(api.chatGPTGenerationSnapshot().state, 'stabilizing');

  api.setGenerationConflictSinceForTest(Date.now() - (api.generationStabilizeMs + 1000));
  const snap = api.chatGPTGenerationSnapshot();
  assert.strictEqual(snap.state, 'conflict');
  assert.strictEqual(snap.conflict, true);
  assert.strictEqual(snap.diagnostic, 'generation_truth_conflict');
  assert.strictEqual(snap.generating, false,
    'an unresolvable contradiction must not pin the campaign at BUSY forever');
  assert.strictEqual(api.superCompactAutoLabel(), 'ATTN',
    'the operator gets a real failure state instead of an eternal activity lie');
});

// ===========================================================================
// Milestone K: "not generating" is not "successfully completed"
// ===========================================================================

test('W7-008: an interrupted assistant is non-generating but still interrupted', () => {
  const { h, api } = campaignWindow();
  const user = userTurn(h, 'u-core', CORE_TURN);
  const assistant = assistantTurn(h, 'a-core');
  assistant._text = 'Stopped thinking';
  addTurns(h, [user, assistant]);

  api.autoRuntime.stage = 'wait-core';
  api.setWaveUserId('core', 'u-core');
  assert.strictEqual(api.saveAutoRuntime({ pauseOnFailure: false }), true);

  assert.strictEqual(api.chatGPTIsGenerating(), false);
  const interrupted = api.interruptedAuditResponseSnapshot();
  assert.strictEqual(interrupted.interrupted, true,
    'a cut-short answer must never read as a completed wave just because generation ended');
});

// ===========================================================================
// Milestone L: ordinary chat regression
// ===========================================================================

test('W7-008: an ordinary completed chat is not BUSY', () => {
  const { h, api } = campaignWindow();
  api.autoRuntime = api.emptyAutoRuntime({ enabled: false, profileId: 'quick3' });
  addTurns(h, [userTurn(h, 'u1', 'hello there'), withFinalActions(h, assistantTurn(h, 'a1'))]);
  assert.strictEqual(api.chatGPTIsGenerating(), false);
  assert.strictEqual(api.superCompactAutoLabel(), 'CHAT',
    'an ordinary conversation is CHAT, never an adopted audit and never BUSY');
});

test('W7-008: ordinary active generation is never adopted into A3', () => {
  const { h, api, fixture } = campaignWindow();
  api.autoRuntime = api.emptyAutoRuntime({ enabled: false, profileId: 'quick3' });
  addTurns(h, [userTurn(h, 'u1', 'hello there'), assistantTurn(h, 'a1')]);
  fixture.stop.hidden = false;
  assert.strictEqual(api.chatGPTIsGenerating(), true,
    'ordinary active generation is still generation');
  assert.strictEqual(api.superCompactAutoLabel(), 'CHAT',
    'generation truth must not adopt an unmanaged conversation into A3');
});
