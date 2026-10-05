'use strict';

// T-259: the live generation control of the CURRENT chatgpt.com build is
// <button type="button" aria-label="Stop"> -- no data-testid, no id, no role.
//
// Captured 2026-09-29 from the real site in the AUDAPACK dedicated Chromium
// profile, mid-generation, with a live turn running:
//
//   form.relative.flex.flex-col.gap-2          (no data-type="unified-composer")
//     editor: div[contenteditable][role=textbox].ProseMirror[aria-label="Ask ChatGPT"]
//     div.ComposerLayoutFooter-_8IVRO
//       div.min-w-0.max-sm:col-start-3.max-sm:row-start-2
//         div.flex.min-w-0.items-center.justify-end.shrink-0
//           div.flex.shrink-0.items-center.gap-2
//             div.flex.items-center
//               button[type=button][aria-label="Stop"]        <- the live Stop
//                 svg.icon-primary-action[aria-hidden=true]   <- the square glyph
//               button[aria-label="Dictate"][disabled]
//             button[aria-label="Select ChatGPT model"]
//     button[aria-label="Add files and more"]                  <- leading slot
//
// T-257 tightened CHATGPT_STOP_SELECTOR to three exact identities
// (data-testid="stop-button", aria-label="Stop generating",
// aria-label="Stop streaming") to kill a false positive. None of them is this
// control, so the tightening removed the real one: two live audit tabs both
// generating, both widgets reading READY. Ownership was never the blocker --
// chatGPTStopNearComposer() accepts root.contains() and the live Stop IS inside
// the composer form. Only identity rejected it.
//
// The whole defect class this file exists to eliminate: a detector whose exact
// identity list is one ChatGPT build behind the site, which fails SAFE (no
// generation) and therefore reads as idle instead of generating.

const { test } = require('node:test');
const assert = require('node:assert');
const {
  setup, userTurn, assistantTurn, addTurns, composerFixture
} = require('./helpers');

const RUN_ID = 'run-stop-current';
const CONVO = 'c:stop-current';

const CORE_TURN = [
  'AUDIT CORE — wave 1/3 of Quick 3 Waves.',
  `CAMPAIGN_RUN_ID: ${RUN_ID}`,
  'ACB_CHAIN_RECEIPT: startcore-stop-1'
].join('\n');

// The composer exactly as the capture found it: the build's form carries no
// data-type, the editor is a ProseMirror div labelled "Ask ChatGPT", and the
// action slot holds the leading attach control, the model picker, a DISABLED
// Dictate and -- while generating -- the Stop.
function currentChatGPTComposer(h, options = {}) {
  const main = require('./helpers').mainEl(h);
  const form = h.el('form', { class: 'relative flex flex-col gap-2' });
  const input = h.el('div', {
    contenteditable: 'true',
    role: 'textbox',
    'aria-label': 'Ask ChatGPT',
    class: 'ProseMirror ProseMirror-focused'
  });
  input.isContentEditable = true;
  form.appendChild(input);

  const footer = h.el('div', { class: 'ComposerLayoutFooter-_8IVRO' });
  const cell = h.el('div', { class: 'min-w-0 max-sm:col-start-3 max-sm:row-start-2' });
  const right = h.el('div', { class: 'flex min-w-0 items-center justify-end shrink-0' });
  const group = h.el('div', { class: 'flex shrink-0 items-center gap-2' });
  const slot = h.el('div', { class: 'flex items-center' });

  const stop = h.el('button', {
    type: 'button',
    'aria-label': 'Stop',
    class: 'cursor-interaction size-token-button-composer flex items-center justify-center rounded-full'
  });
  stop.appendChild(h.el('svg', { class: 'icon-primary-action text-composer-primary', 'aria-hidden': 'true' }));
  stop.hidden = options.generating === false;
  slot.appendChild(stop);
  slot.appendChild(h.el('button', { type: 'button', 'aria-label': 'Dictate', disabled: 'true' }));
  group.appendChild(slot);
  right.appendChild(group);
  right.appendChild(h.el('button', { type: 'button', 'aria-label': 'Select ChatGPT model' }));
  cell.appendChild(right);
  footer.appendChild(cell);
  form.appendChild(footer);
  form.appendChild(h.el('button', { type: 'button', 'aria-label': 'Add files and more' }));
  main.appendChild(form);

  const fixture = composerFixture(h);
  fixture.form.remove();
  fixture.stop.remove();
  return { form, input, stop, legacy: fixture };
}

function activeCoreWindow(profileId = 'quick3', options = {}) {
  const { h, api } = setup();
  currentChatGPTComposer(h, options);
  api.state.auditProfile = profileId;
  api.state.chatgptPromptDelivery = 'text';
  api.autoRuntime = api.emptyAutoRuntime({ enabled: true, profileId });
  api.autoRuntime.conversationKey = CONVO;
  api.autoRuntime.runId = RUN_ID;
  api.autoRuntime.startedAt = Date.now();
  api.autoRuntime.stage = 'wait-core';
  api.setWaveUserId('core', 'u-core');
  return { h, api };
}

function completedAssistant(h, id) {
  const turn = assistantTurn(h, id);
  const body = h.el('div', { 'data-message-content-part-type': 'text' });
  body._text = 'AUDIT CORE handoff body';
  turn.appendChild(body);
  const actions = h.el('div', { 'aria-label': 'Response actions' });
  actions.appendChild(h.el('button', {
    'data-testid': 'copy-turn-action-button',
    'aria-label': 'Copy response'
  }));
  turn.appendChild(actions);
  return turn;
}

// ===========================================================================
// The production screenshot, as a test
// ===========================================================================

test('W7-009: the real current Stop in the real current composer IS generation', () => {
  const { api } = activeCoreWindow();
  const snap = api.chatGPTGenerationSnapshot();
  assert.strictEqual(snap.generating, true,
    'the live aria-label="Stop" inside the current composer is model generation');
  assert.strictEqual(snap.state, 'generating');
  assert.strictEqual(snap.inside_composer_root, true);
  assert.strictEqual(snap.reason, 'canonical-stop-owned');
});

test('W7-009: an actively generating Core is never READY', () => {
  const { api } = activeCoreWindow();
  assert.notStrictEqual(api.superCompactAutoLabel(), 'READY',
    'READY is forbidden while generation is actually in progress');
  assert.strictEqual(api.superCompactAutoLabel(), 'CORE');
});

test('W7-009: an ordinary non-audit chat generating never reads READY', () => {
  const { h, api } = setup();
  currentChatGPTComposer(h);
  assert.strictEqual(api.chatGPTIsGenerating(), true);
  // No audit runtime, so the label is the ordinary-chat token; the invariant
  // under test is only that a live Stop can never be projected as READY.
  assert.strictEqual(api.superCompactAutoLabel(), 'CHAT');
  assert.notStrictEqual(api.superCompactAutoLabel(), 'READY');
});

// Not project-specific: a second profile, and a second independent composer,
// must both see the same generation truth.
test('W7-009: a second project and a second composer read the same truth', () => {
  const super10 = activeCoreWindow('super10');
  assert.strictEqual(super10.api.chatGPTIsGenerating(), true);
  assert.notStrictEqual(super10.api.superCompactAutoLabel(), 'READY');

  const { h, api } = setup();
  currentChatGPTComposer(h);
  const first = currentChatGPTComposer(h);
  first.form.remove();
  assert.strictEqual(api.chatGPTIsGenerating(), true,
    'only the mounted composer owns the Stop');
});

// ===========================================================================
// The direction T-257 fixed must stay fixed
// ===========================================================================

test('W7-009: no Stop and final actions mounted is a finished answer, not generating', () => {
  const { h, api } = activeCoreWindow('quick3', { generating: false });
  addTurns(h, [userTurn(h, 'u-core', CORE_TURN), completedAssistant(h, 'a-core')]);
  const snap = api.chatGPTGenerationSnapshot();
  assert.strictEqual(snap.generating, false);
  assert.strictEqual(snap.latest_assistant_has_final_actions, true);
  assert.notStrictEqual(snap.state, 'generating');
  assert.strictEqual(api.superCompactAutoLabel(), 'CORE',
    'a completed answer still reports its wave token, never an eternal BUSY');
});

test('W7-009: an exact Stop outside the composer is still not generation', () => {
  const { h, api } = activeCoreWindow('quick3', { generating: false });
  h.dom.body.appendChild(h.el('button', { type: 'button', 'aria-label': 'Stop' }));
  assert.strictEqual(api.chatGPTIsGenerating(), false,
    'identity alone is not ownership');
});

test('W7-009: an exact Stop in a conversation turn is still not generation', () => {
  const { h, api } = activeCoreWindow('quick3', { generating: false });
  const turn = assistantTurn(h, 'a-1');
  turn.appendChild(h.el('button', { type: 'button', 'aria-label': 'Stop' }));
  addTurns(h, [userTurn(h, 'u-1', 'hi'), turn]);
  assert.strictEqual(api.chatGPTIsGenerating(), false);
});

test('W7-009: voice controls are still not generation', () => {
  const { h, api } = activeCoreWindow('quick3', { generating: false });
  const voice = h.el('div', { 'data-testid': 'voice-mode-panel' });
  voice.appendChild(h.el('button', { 'aria-label': 'Stop listening' }));
  h.dom.body.appendChild(voice);
  assert.strictEqual(api.chatGPTIsGenerating(), false);
});

test('W7-009: a hidden stale Stop from an unmounted composer is not generation', () => {
  const { h, api } = activeCoreWindow('quick3', { generating: false });
  const stale = h.el('button', { type: 'button', 'aria-label': 'Stop' });
  stale.hidden = true;
  h.dom.body.appendChild(stale);
  assert.strictEqual(api.chatGPTIsGenerating(), false);
});
