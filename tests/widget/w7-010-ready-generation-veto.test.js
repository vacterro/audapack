'use strict';

// T-260: READY is a claim -- "this tab is idle and ready for an audit" -- and it
// is only that claim when the live-generation veto passed.
//
// The operator screenshot after 0.0.86: a ChatGPT tab visibly generating an A3
// audit, wave indicators lit, and the compact cell reading READY. The tab could
// not be inspected from outside (the managed worker opens no debugging port) and
// the installed build could not be read from the screenshot, so the widget was
// lying in a way nobody could audit from the outside.
//
// The defect class this file exists to eliminate: a detector that cannot
// identify what it sees, reporting its FAILURE TO IDENTIFY as a positive
// "nothing is happening". T-257 made the generation verdict strict; this keeps
// that verdict untouched and stops the LABEL from reading over it.

const { test } = require('node:test');
const assert = require('node:assert');
const {
  setup, userTurn, assistantTurn, addTurns
} = require('./helpers');

const RUN_ID = 'run-ready-veto';

// A real-current-build composer: form without data-type, ProseMirror editor
// labelled "Ask ChatGPT", trailing action slot, leading attach control.
function currentComposer(h) {
  const main = require('./helpers').mainEl(h);
  const form = h.el('form', { class: 'relative flex flex-col gap-2' });
  const input = h.el('div', {
    contenteditable: 'true',
    role: 'textbox',
    'aria-label': 'Ask ChatGPT',
    class: 'ProseMirror'
  });
  input.isContentEditable = true;
  form.appendChild(input);
  const slot = h.el('div', { class: 'flex items-center' });
  form.appendChild(slot);
  form.appendChild(h.el('button', { type: 'button', 'aria-label': 'Add files and more' }));
  main.appendChild(form);
  return { form, input, slot };
}

function idleAuditWindow(options = {}) {
  const { h, api } = setup();
  const composer = currentComposer(h);
  api.state.auditProfile = 'quick3';
  api.state.chatgptPromptDelivery = 'text';
  api.autoRuntime = api.emptyAutoRuntime({ enabled: true, profileId: 'quick3' });
  api.autoRuntime.conversationKey = 'c:ready-veto';
  api.autoRuntime.runId = RUN_ID;
  api.autoRuntime.startedAt = Date.now();
  api.autoRuntime.stage = 'idle';
  api.setWaveUserId('core', 'u-core');
  if (options.userTurn !== false) {
    addTurns(h, [userTurn(h, 'u-core', 'AUDIT CORE — wave 1/3 of Quick 3 Waves.')]);
  }
  return { h, api, composer };
}

// The verified current Stop: the label returns to its exact truth immediately.
test('W7-010: the real current Stop is generation, not a veto', () => {
  const { h, api, composer } = idleAuditWindow();
  const stop = h.el('button', { type: 'button', 'aria-label': 'Stop' });
  composer.slot.appendChild(stop);
  assert.strictEqual(api.chatGPTGenerationSnapshot().generating, true);
  // The audit lineage is on screen, so the live wave token is the label --
  // what matters here is that it is not READY.
  assert.strictEqual(api.superCompactAutoLabel(), 'CORE');
});

// The live symptom: the widget finds generation-shaped chrome in the CURRENT
// composer, cannot identify it, and used to answer READY.
test('W7-010: an unidentified stop control in the composer is never READY', () => {
  const { h, api, composer } = idleAuditWindow();
  const unknown = h.el('button', { type: 'button', 'aria-label': 'Stop responding' });
  composer.slot.appendChild(unknown);

  const snap = api.chatGPTGenerationSnapshot();
  assert.strictEqual(snap.generating, false,
    'an unverified control never becomes generation -- T-257 stays closed');
  assert.strictEqual(snap.rejected_in_composer, true,
    'but the disagreement with "idle" is recorded');
  assert.strictEqual(api.superCompactAutoLabel(), 'ATTN',
    'READY over a rejected in-composer stop control is the defect itself');
});

test('W7-010: a future build naming the Stop differently still cannot read READY', () => {
  const { h, api, composer } = idleAuditWindow();
  composer.slot.appendChild(h.el('button', { type: 'button', 'data-testid': 'stop-response' }));
  assert.strictEqual(api.chatGPTGenerationSnapshot().rejected_in_composer, true);
  assert.notStrictEqual(api.superCompactAutoLabel(), 'READY');
});

test('W7-010: a rejected stop OUTSIDE the composer never vetoes READY', () => {
  const { h, api } = idleAuditWindow();
  h.dom.body.appendChild(h.el('button', { type: 'button', 'aria-label': 'Stop responding' }));
  assert.strictEqual(api.chatGPTGenerationSnapshot().rejected_in_composer, false);
  assert.strictEqual(api.superCompactAutoLabel(), 'READY');
});

// The 0.0.85 stall must not come back: a control the widget already classifies
// as non-generation stays silent, even inside the composer's own shell.
test('W7-010: voice, dictation and recording controls never veto READY', () => {
  for (const attrs of [
    { 'aria-label': 'Stop listening' },
    { 'aria-label': 'Stop recording' },
    { 'data-testid': 'stop-listening' },
    { 'aria-label': 'Stop voice' }
  ]) {
    const { h, api, composer } = idleAuditWindow();
    composer.slot.appendChild(h.el('button', attrs));
    assert.strictEqual(api.chatGPTGenerationSnapshot().rejected_in_composer, false,
      `${JSON.stringify(attrs)} is a non-generation control`);
    assert.strictEqual(api.superCompactAutoLabel(), 'READY', JSON.stringify(attrs));
  }
});

test('W7-010: the 0.0.85 sibling-shell stall still reads its wave, not ATTN', () => {
  const { h, api, composer } = idleAuditWindow();
  const shell = h.el('div', {});
  shell.appendChild(h.el('button', { 'data-testid': 'stop-listening', 'aria-label': 'Stop listening' }));
  composer.form.parentNode.appendChild(shell);
  assert.strictEqual(api.chatGPTIsGenerating(), false);
  assert.strictEqual(api.superCompactAutoLabel(), 'READY');
});

// The veto is a label rule, never a wave-commit rule.
test('W7-010: the veto never turns an unverified control into a generation', () => {
  const { h, api, composer } = idleAuditWindow();
  composer.slot.appendChild(h.el('button', { type: 'button', 'aria-label': 'Stop responding' }));
  assert.strictEqual(api.chatGPTIsGenerating(), false);
  assert.strictEqual(api.chatGPTGenerationSnapshot().conflict, false,
    'an unverified candidate is not a truth conflict either');
});

// A finished answer with a stale in-composer control keeps reporting its wave:
// the veto only ever replaces READY.
test('W7-010: a completed Core still reports CORE, never ATTN or READY', () => {
  const { h, api, composer } = idleAuditWindow();
  api.autoRuntime.stage = 'wait-core';
  const assistant = assistantTurn(h, 'a-core');
  const body = h.el('div', { 'data-message-content-part-type': 'text' });
  body._text = 'handoff body';
  assistant.appendChild(body);
  const actions = h.el('div', { 'aria-label': 'Response actions' });
  actions.appendChild(h.el('button', {
    'data-testid': 'copy-turn-action-button', 'aria-label': 'Copy response'
  }));
  assistant.appendChild(actions);
  addTurns(h, [assistant]);
  composer.slot.appendChild(h.el('button', { type: 'button', 'aria-label': 'Stop responding' }));
  assert.strictEqual(api.superCompactAutoLabel(), 'CORE');
});
