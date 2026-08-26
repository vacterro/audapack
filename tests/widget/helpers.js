'use strict';

const { createHarness, FakeEvent } = require('./harness');

function setup() {
  const h = createHarness();
  const api = h.load();
  if (h.loadError) throw h.loadError;
  return { h, api };
}

function mainEl(h) {
  return h.dom.documentElement.querySelector('main');
}

function userTurn(h, id, text) {
  const el = h.el('article', {
    'data-message-author-role': 'user',
    'data-testid': `conversation-turn-${id}`,
    'data-message-id': id
  });
  if (text) el._text = text;
  return el;
}

function assistantTurn(h, id, build) {
  const el = h.el('article', {
    'data-message-author-role': 'assistant',
    'data-testid': `conversation-turn-${id}`,
    'data-message-id': id
  });
  if (build) build(el);
  return el;
}

function addTurns(h, turns) {
  const main = mainEl(h);
  for (const turn of turns) main.appendChild(turn);
}

function composerFixture(h) {
  const main = mainEl(h);
  const form = h.el('form', { 'data-type': 'unified-composer' });
  const input = h.el('div', {
    id: 'prompt-textarea',
    contenteditable: 'true',
    role: 'textbox',
    'aria-label': 'Chat with ChatGPT'
  });
  input.isContentEditable = true;
  const send = h.el('button', { 'data-testid': 'send-button' });
  const stop = h.el('button', { 'aria-label': 'Stop generating' });
  stop.hidden = true;
  form.appendChild(input);
  form.appendChild(send);
  form.appendChild(stop);
  main.appendChild(form);
  return { form, input, send, stop };
}

function leaseFor(h, key, ownerId, nonce, expiresAt) {
  h.api.storage.gmSet(`${h.api.constants.AUTO_LEASE_PREFIX}${key}`, JSON.stringify({
    version: 1,
    ownerId,
    conversationKey: key,
    nonce,
    expiresAt,
    updatedAt: Date.now()
  }));
}

function runtimeFixture(overrides = {}) {
  return {
    version: 4,
    enabled: true,
    stage: 'idle',
    conversationKey: 'c:abc123',
    anchorUserId: '',
    seenUserId: '',
    coreUserId: '',
    secondUserId: '',
    performanceUserId: '',
    expectedKind: '',
    pendingSendReceipt: '',
    pendingSendKind: '',
    pendingSendPreviousUserId: '',
    pendingSendStartedAt: 0,
    pendingSendRetries: 0,
    pausedReason: '',
    pausedFromStage: '',
    startedAt: 0,
    waitStartedAt: 0,
    stableResponseKey: '',
    stableSince: 0,
    continuationReason: '',
    continuationKind: '',
    continuationPreviousUserId: '',
    stallNudges: {},
    partialContinuations: {},
    retryClicks: {},
    continueGeneratingClicks: {},
    ...overrides
  };
}

module.exports = { setup, mainEl, userTurn, assistantTurn, addTurns, composerFixture, leaseFor, runtimeFixture, FakeEvent };