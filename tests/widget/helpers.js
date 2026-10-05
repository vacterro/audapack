'use strict';

const { createHarness, FakeEvent } = require('./harness');

function setup(options = {}) {
  const h = createHarness(options);
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
  const upload = h.el('input', { id: 'upload-files', type: 'file', multiple: 'true' });
  const send = h.el('button', { 'data-testid': 'send-button' });
  const stop = h.el('button', { 'aria-label': 'Stop generating' });
  stop.hidden = true;
  form.appendChild(input);
  form.appendChild(upload);
  form.appendChild(send);
  form.appendChild(stop);
  main.appendChild(form);
  return { form, input, upload, send, stop };
}

function addComposerAttachmentTile(h, name, options = {}) {
  const form = h.dom.documentElement.querySelector('form[data-type="unified-composer"]');
  if (!form) throw new Error('composer fixture missing');
  const tile = h.el('div', { role: 'group', 'aria-label': name });
  const remove = h.el('button', { 'aria-label': `Remove file ${name}` });
  tile.appendChild(remove);
  if (options.busy) tile.appendChild(h.el('span', { class: 'animate-spin' }));
  form.appendChild(tile);
  return tile;
}

// A click alone is never Send acceptance. Real ChatGPT consumes the submitted
// composer payload and adds one user turn carrying it (machine receipt
// included), and the widget's positive verification observes that transition --
// never the click. `consumeAttachment` also models the attachment tiles leaving
// the composer, which is how an attachment-only payload proves acceptance.
function installAcceptedSend(h, fixture, options = {}) {
  fixture.send.addEventListener('click', () => {
    // `_sendFails` is the harness hook for a ChatGPT submission that is
    // refused/unacknowledged: the click happens, but the composer is untouched.
    if (fixture.send._sendFails) return;
    const text = String(fixture.input.textContent || '');
    const tiles = Array.from(fixture.form.children).filter(child =>
      child.getAttribute && child.getAttribute('role') === 'group');
    if (!text.trim() && !tiles.length) return;
    fixture._submitted = text;
    fixture.input.textContent = '';
    if (options.consumeAttachment) {
      for (const tile of tiles) tile.remove();
    }
    const index = (Number(fixture._acceptedTurns) || 0) + 1;
    fixture._acceptedTurns = index;
    const turn = h.el('article', {
      'data-message-author-role': 'user',
      'data-testid': `conversation-turn-${index}`,
      'data-message-id': `accepted-${index}`
    });
    turn._text = text || tiles.map(tile => tile.getAttribute('aria-label')).join(', ');
    mainEl(h).appendChild(turn);
    h.mutate(fixture.form);
  });
  return fixture;
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

module.exports = { setup, mainEl, userTurn, assistantTurn, addTurns, composerFixture, addComposerAttachmentTile, installAcceptedSend, leaseFor, runtimeFixture, FakeEvent };
