'use strict';

const { test } = require('node:test');
const assert = require('node:assert');
const { setup, mainEl, userTurn, assistantTurn, addTurns, composerFixture } = require('./helpers');

function authoredAssistant(h) {
  return assistantTurn(h, 'a1', el => {
    const markdown = h.el('div', { class: 'markdown' });
    markdown._text = 'The complete report for CORE is ready.';
    el.appendChild(markdown);
  });
}

test('W2-007: authored markdown content is recognized and non-authored chrome is not', () => {
  const { h, api } = setup();
  const main = mainEl(h);
  const turn = authoredAssistant(h);
  const markdown = turn.querySelector('.markdown');
  assert.strictEqual(api.isAuthoredAssistantContent(markdown), true);
  assert.strictEqual(api.isAuthoredAssistantContent(turn), false);
  const looseButton = h.el('button', { 'aria-label': 'Retry' });
  main.appendChild(looseButton);
  assert.strictEqual(api.isAuthoredAssistantContent(looseButton), false);
});

test('W2-007: retry by explicit aria-label within the turn', () => {
  const { h, api } = setup();
  const turn = authoredAssistant(h);
  const actions = h.el('div', { 'aria-label': 'Response actions' });
  const retry = h.el('button', { 'aria-label': 'Retry response' });
  actions.appendChild(retry);
  turn.appendChild(actions);
  addTurns(h, [turn]);
  assert.strictEqual(api.findAssistantRecoveryControl(turn, 'retry'), retry);
});

test('W2-007: generic Retry/Try again only trusted inside response actions', () => {
  const { h, api } = setup();
  const main = mainEl(h);

  const authoredTurn = authoredAssistant(h);
  const authoredButton = h.el('button', { 'aria-label': 'Try again' });
  authoredTurn.appendChild(authoredButton);
  addTurns(h, [authoredTurn]);
  assert.strictEqual(api.findAssistantRecoveryControl(authoredTurn, 'retry'), null, 'authored Try again must not be clicked');

  const chromeTurn = assistantTurn(h, 'a2', el => {
    const actions = h.el('div', { 'data-testid': 'response-actions' });
    const tryAgain = h.el('button', { 'aria-label': 'Try again' });
    actions.appendChild(tryAgain);
    el.appendChild(actions);
  });
  addTurns(h, [chromeTurn]);
  assert.strictEqual(api.findAssistantRecoveryControl(chromeTurn, 'retry').getAttribute('aria-label'), 'Try again');
});

test('W2-007: retry via data-testid', () => {
  const { h, api } = setup();
  const turn = assistantTurn(h, 'a1', el => {
    const retry = h.el('button', { 'data-testid': 'retry-button' });
    el.appendChild(retry);
  });
  addTurns(h, [turn]);
  assert.strictEqual(api.findAssistantRecoveryControl(turn, 'retry'), turn.querySelector('[data-testid="retry-button"]'));
});

test('W2-007: continue generating button found and detection helpers agree', () => {
  const { h, api } = setup();
  const turn = assistantTurn(h, 'a1', el => {
    const cont = h.el('button', { 'data-testid': 'continue-generating' });
    el.appendChild(cont);
  });
  addTurns(h, [turn]);
  assert.strictEqual(api.assistantNeedsContinuation(turn), true);
  assert.strictEqual(api.assistantHasRetryError(turn), false);
  assert.strictEqual(api.assistantContinueGeneratingButton(turn).getAttribute('data-testid'), 'continue-generating');
});

test('W2-007: continue via explicit aria-label', () => {
  const { h, api } = setup();
  const turn = assistantTurn(h, 'a1', el => {
    const cont = h.el('button', { 'aria-label': 'Continue generating' });
    el.appendChild(cont);
  });
  addTurns(h, [turn]);
  assert.strictEqual(api.assistantNeedsContinuation(turn), true);
});

test('W2-007: recovery refuses to run without a verifiable lease', async () => {
  const { h, api } = setup();
  composerFixture(h);
  api.autoRuntime.enabled = false;
  const turn = assistantTurn(h, 'a1', el => {
    const retry = h.el('button', { 'aria-label': 'Retry response' });
    el.appendChild(retry);
  });
  addTurns(h, [turn]);
  const promise = api.autoClickAssistantRecovery(turn, 'retry', 'core');
  await h.settle();
  assert.strictEqual(await promise, false);
  assert.strictEqual(turn.querySelector('button')._clicked, undefined);
});

test('W2-007: recovery clicks retry once and persists the counter', async () => {
  const { h, api } = setup();
  composerFixture(h);
  api.autoRuntime.enabled = true;
  const turn = assistantTurn(h, 'a1', el => {
    const retry = h.el('button', { 'aria-label': 'Retry response' });
    el.appendChild(retry);
  });
  addTurns(h, [turn]);
  const promise = api.autoClickAssistantRecovery(turn, 'retry', 'core');
  await h.settle();
  assert.strictEqual(await promise, true);
  assert.strictEqual(turn.querySelector('button')._clicked, true);
  assert.strictEqual(api.autoRuntime.retryClicks.core, 1);
  const stored = api.loadAutoRuntime(api.autoBoundConversationKey);
  assert.strictEqual(stored.retryClicks.core, 1, 'counter must be persisted');
});

test('W2-007: retry safety cap pauses the chain and does not click', async () => {
  const { h, api } = setup();
  composerFixture(h);
  api.autoRuntime.enabled = true;
  api.autoRuntime.retryClicks = { core: 3 };
  const turn = assistantTurn(h, 'a1', el => {
    const retry = h.el('button', { 'aria-label': 'Retry response' });
    el.appendChild(retry);
  });
  addTurns(h, [turn]);
  const promise = api.autoClickAssistantRecovery(turn, 'retry', 'core');
  await h.settle();
  assert.strictEqual(await promise, false);
  assert.strictEqual(turn.querySelector('button')._clicked, undefined);
  assert.strictEqual(api.autoRuntime.stage, 'paused', 'cap must pause automation');
  assert.ok(api.autoRuntime.pausedReason.includes('safety cap'));
});

test('W2-007: continue generating click bumps its own counter', async () => {
  const { h, api } = setup();
  composerFixture(h);
  api.autoRuntime.enabled = true;
  const turn = assistantTurn(h, 'a1', el => {
    const cont = h.el('button', { 'aria-label': 'Continue generating' });
    el.appendChild(cont);
  });
  addTurns(h, [turn]);
  const promise = api.autoClickAssistantRecovery(turn, 'continue', 'core');
  await h.settle();
  assert.strictEqual(await promise, true);
  assert.strictEqual(turn.querySelector('button')._clicked, true);
  assert.strictEqual(api.autoRuntime.continueGeneratingClicks.core, 1);
});