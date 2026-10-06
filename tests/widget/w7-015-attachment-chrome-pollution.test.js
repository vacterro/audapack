'use strict';

// The 2026-10 ChatGPT build puts the attachment tiles INSIDE the turn container.
//
// Measured live on dsp-fc76adbfbfc649e7 (FastPrompter quick3, one worker,
// widget 0.0.96) through CDP on the DEDICATED AUDAPACK profile. Turn discovery
// was already fixed by w7-014, and with it the diagnostic verdict went NONE --
// yet the widget's own state line stayed
//   "Armed. Waiting for a NEW AUDIT CORE. Active chain state is present"
// with Implementation handoff: IDLE and an empty 1/2/3 progress row, and the
// Bridge job never left STARTED (it never posted AUDITING). The Core, its
// receipt and ChatGPT's finished 16443-char answer were all on screen.
//
// The cause is text, not discovery. One probe of the live user message:
//   container textContent      5136 chars
//   [data-user-message-bubble] 5099 chars      <- the authored Core
//   difference                   37 chars      <- tile chrome
// and the first content line of the container was
//   "FastPrompter_04.10.26-T08-01-13.zipFileA..."
// The tiles row and its "File attached"/"Show" affordances are siblings of the
// bubble, so container textContent opens with the archive FILENAME and carries
// no separator before the prose.
//
// classifyAuditMessage deliberately trusts only the FIRST meaningful line --
// later lines must never promote ordinary discussion into a command. So on this
// build every managed START turn opened with the ZIP filename, matched no
// marker, and returned ''. The turn classified as a NON-audit turn, which
// visibleAuditLineage treats as a hard lineage barrier that severs the chain.
// The widget therefore never adopted a Core it had authored itself.
//
// This is an ordinary DOM bug, never an external blocker.

const { test } = require('node:test');
const assert = require('node:assert');
const { setup, mainEl } = require('./helpers');

const RECEIPT = 'startcore-mutcsiae-b9785960938e';
const USER_MSG_ID = '63d00465-3614-4c89-a037-c3a7b96d6bff';

const CORE_BODY = [
  'AUDIT CORE - wave 1/3 of Quick 3 Waves.',
  'CAMPAIGN_RUN_ID: run-w7-015',
  'PROJECT_NAME: FastPrompter',
  'ACB_CHAIN_RECEIPT: ' + RECEIPT
].join('\n');

// Verbatim from the live page: the tile label and its affordances, with NO
// separator before the authored prose, because textContent just concatenates.
const TILE_CHROME = 'FastPrompter_04.10.26-T08-01-13.zipFileA…Show';

function currentBuildUserMessageWithTiles(h, body) {
  const message = h.el('div', { 'data-chatgpt-search-message-ids': USER_MSG_ID });
  const group = h.el('div', { class: 'group/user-message' });
  const tilesRow = h.el('div', { class: 'flex w-full flex-wrap' });
  tilesRow._text = TILE_CHROME;
  const bubbleWrap = h.el('div', { class: 'w-full' });
  const bubbleWrap2 = h.el('div', { class: 'bg-user-message' });
  const bubble = h.el('div', { 'data-user-message-bubble': '' });
  bubble._text = body;
  bubbleWrap2.appendChild(bubble);
  bubbleWrap.appendChild(bubbleWrap2);
  group.appendChild(tilesRow);
  group.appendChild(bubbleWrap);
  message.appendChild(group);
  return message;
}

function assistantMessage(h, text) {
  const message = h.el('div', {
    'data-chatgpt-search-message-ids': 'a1910972-c34d-445c-88ef-9ee8dc99d181 a19'
  });
  const heading = h.el('h4', { 'data-conversation-role': 'assistant' });
  heading._text = 'ARCHITECTURE';
  const body = h.el('div', { 'data-chatgpt-selection-message-id': 'a1910972-c34d-445c-88ef-9ee8dc99d181' });
  body._text = text;
  message.appendChild(heading);
  message.appendChild(body);
  return message;
}

function mountThread(h, messages) {
  const main = mainEl(h);
  const scroll = h.el('div', { 'data-app-action-timeline-scroll': '' });
  const findTarget = h.el('div', { 'data-thread-find-target': '' });
  for (const message of messages) findTarget.appendChild(message);
  scroll.appendChild(findTarget);
  main.appendChild(scroll);
}

test('the container really does open with the archive filename, not the command', () => {
  const { h, api } = setup();
  mountThread(h, [currentBuildUserMessageWithTiles(h, CORE_BODY)]);
  const user = api.getChatGPTTurns().find(turn => api.turnRole(turn) === 'user');

  assert.ok(user, 'the user turn is discovered (w7-014 already fixed this)');
  const container = String(user.textContent || '');
  assert.ok(container.startsWith('FastPrompter_'),
    'the fixture must reproduce the live pollution, or it proves nothing');
  assert.ok(container.includes(RECEIPT),
    'the authored Core is still inside the same container');
});

test('a Core whose container opens with the ZIP filename still classifies as core', () => {
  const { h, api } = setup();
  mountThread(h, [currentBuildUserMessageWithTiles(h, CORE_BODY)]);
  const user = api.getChatGPTTurns().find(turn => api.turnRole(turn) === 'user');

  assert.strictEqual(api.classifyAuditTurn(user), 'core',
    'tile chrome is not an authored line and must not hide the command under it');
});

test('the polluted Core is adopted as a lineage root, not a chain-breaking barrier', () => {
  const { h, api } = setup();
  mountThread(h, [
    currentBuildUserMessageWithTiles(h, CORE_BODY),
    assistantMessage(h, 'ARCHITECTURE\nwave 1 complete')
  ]);

  const lineage = api.visibleAuditLineage();
  assert.strictEqual(Boolean(lineage.blockedByReset), false);
  assert.ok(lineage.core,
    'the Core is on screen and must resolve as the wave-1 root');
  assert.strictEqual(api.turnRole(lineage.core), 'user');
});

test('the authored prose is read without the tile chrome in front of it', () => {
  const { h, api } = setup();
  mountThread(h, [currentBuildUserMessageWithTiles(h, CORE_BODY)]);
  const user = api.getChatGPTTurns().find(turn => api.turnRole(turn) === 'user');

  const first = api.userTurnTextCandidates(user)[0] || '';
  assert.ok(first.startsWith('AUDIT CORE'),
    'the first readable representation must be the authored command itself');
  assert.ok(!first.startsWith('FastPrompter_'),
    'the archive filename must not lead the authored text');
});

test('a historic build turn keeps classifying exactly as before', () => {
  const { h, api } = setup();
  const historic = h.el('article', {
    'data-message-author-role': 'user',
    'data-testid': 'conversation-turn-7',
    'data-message-id': 'm7'
  });
  historic._text = CORE_BODY;
  mainEl(h).appendChild(historic);

  const user = api.getChatGPTTurns().find(turn => api.turnRole(turn) === 'user');
  assert.strictEqual(api.classifyAuditTurn(user), 'core');
});
