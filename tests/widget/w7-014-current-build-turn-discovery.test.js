'use strict';

// The 2026-10 ChatGPT build renamed every attribute the turn reader knew.
//
// Measured live on dsp-fc76adbfbfc649e7 (FastPrompter quick3, one worker,
// widget 0.0.95) through a CDP session on the DEDICATED AUDAPACK profile. The
// Core was composed, the ZIP was attached, the Send was clicked and ACCEPTED:
// the conversation https://chatgpt.com/c/6ac1dd96-... rendered the authored
// Core as a 5099-char user message carrying
//   ACB_CHAIN_RECEIPT: startcore-mutcsiae-b9785
// and ChatGPT answered with a 16441-char ARCHITECTURE turn. Every transport
// stage worked and the run still never advanced: for minutes every diagnostic
// read
//   lineage=TURN_DISCOVERY reason=no-user-turn-discovered turns=0(u0/a0)
//
// getChatGPTTurns() matched two shapes and the build has neither:
//   [data-turn], [data-testid^="conversation-turn-"], [data-message-author-role]
// A whole-conversation census inside WorkspaceContent found ZERO of them.
//
// The replacement shape, read off the finished conversation:
//   [data-turn-key]                      the CONVERSATION id, ONE per thread
//                                        (63d00465-... == the user message id) --
//                                        NOT a turn, so it must never be read as one
//   [data-chatgpt-search-message-ids]    ONE PER MESSAGE, in document order
//   [data-user-message-bubble]           marks the user half
//   [data-conversation-role="assistant"] marks the answer half
//   [data-chatgpt-selection-message-id]  the assistant message id
//
// A reader that only knows the old names cannot adopt its own Core, cannot
// enter a wave, and cannot harvest an answer. That is the whole A3 failure, and
// it is an ordinary DOM bug, never an external blocker.

const { test } = require('node:test');
const assert = require('node:assert');
const { setup, mainEl } = require('./helpers');

const RECEIPT = 'startcore-mutcsiae-b9785';
const THREAD_KEY = '63d00465-3614-4c89-a037-c3a7b96d6bff';
const USER_MSG_ID = THREAD_KEY;
const ASSISTANT_MSG_ID = 'a1910972-c34d-445c-88ef-9ee8dc99d181';

const CORE_BODY = [
  'AUDIT CORE - wave 1/3 of Quick 3 Waves.',
  'CAMPAIGN_RUN_ID: run-w7-014',
  'PROJECT_NAME: FastPrompter',
  'ACB_CHAIN_RECEIPT: ' + RECEIPT
].join('\n');

// The live wrapper chain, verbatim. Every attribute and nesting below was read
// off the running page; nothing here is a widget convenience.
function currentBuildUserMessage(h, text) {
  const message = h.el('div', { 'data-chatgpt-search-message-ids': USER_MSG_ID });
  const group = h.el('div', { class: 'group/user-message' });
  const bubbleWrap = h.el('div', { class: 'bg-user-message' });
  const bubble = h.el('div', { 'data-user-message-bubble': '' });
  bubble._text = text;
  bubbleWrap.appendChild(bubble);
  group.appendChild(bubbleWrap);
  message.appendChild(group);
  return message;
}

function currentBuildAssistantMessage(h, build) {
  const message = h.el('div', {
    'data-chatgpt-search-message-ids': ASSISTANT_MSG_ID + ' a19'
  });
  const heading = h.el('h4', { 'data-conversation-role': 'assistant' });
  heading._text = 'ARCHITECTURE';
  const body = h.el('div', { 'data-chatgpt-selection-message-id': ASSISTANT_MSG_ID });
  if (build) build(body);
  message.appendChild(heading);
  message.appendChild(body);
  return message;
}

// The thread wrapper: [data-turn-key] spans the WHOLE exchange, so reading it
// as a turn would collapse both halves into one node.
function mountThread(h, messages) {
  const main = mainEl(h);
  const scroll = h.el('div', { 'data-app-action-timeline-scroll': '' });
  const threadKey = h.el('div', { 'data-turn-key': THREAD_KEY });
  const findTarget = h.el('div', { 'data-thread-find-target': '' });
  for (const message of messages) findTarget.appendChild(message);
  threadKey.appendChild(findTarget);
  scroll.appendChild(threadKey);
  main.appendChild(scroll);
  return threadKey;
}

test('both halves of a current-build exchange are discovered as separate turns', () => {
  const { h, api } = setup();
  mountThread(h, [
    currentBuildUserMessage(h, CORE_BODY),
    currentBuildAssistantMessage(h, node => { node._text = 'ARCHITECTURE\nwave 1 complete'; })
  ]);

  const turns = api.getChatGPTTurns();
  assert.strictEqual(turns.length, 2,
    'the user message and the answer are two turns, and the thread wrapper is neither');
  assert.strictEqual(api.turnRole(turns[0]), 'user');
  assert.strictEqual(api.turnRole(turns[1]), 'assistant');
});

test('the current-build user turn is the one holding the exact machine receipt', () => {
  const { h, api } = setup();
  mountThread(h, [
    currentBuildUserMessage(h, CORE_BODY),
    currentBuildAssistantMessage(h, node => { node._text = 'ARCHITECTURE'; })
  ]);

  const user = api.getChatGPTTurns().find(turn => api.turnRole(turn) === 'user');
  assert.ok(user, 'the authored Core turn must be visible to the reader');
  assert.ok(String(user.innerText || '').includes(RECEIPT),
    'the discovered user turn is the composed Core itself');
});

test('a Core registered on the current build is REGISTERED, never REJECTED', async () => {
  const { h, api } = setup();
  mountThread(h, [currentBuildUserMessage(h, CORE_BODY)]);

  // The composer is already empty and the page is NOT generating: nothing but
  // the turn itself can prove the submission. This is the live shape.
  const pending = api.chatGPTSendOutcome(RECEIPT, CORE_BODY, 400, true);
  await h.settle();
  const outcome = await pending;

  assert.strictEqual(outcome.submitted, true,
    'the authored Core turn is in the conversation, so the Send was accepted');
  assert.strictEqual(outcome.state, 'REGISTERED',
    'an exact-receipt user turn with a stable id is a registration');
});

test('the historic build shape still resolves identically', () => {
  const { h, api } = setup();
  const historic = h.el('article', {
    'data-message-author-role': 'user',
    'data-testid': 'conversation-turn-7',
    'data-message-id': 'm7'
  });
  historic._text = CORE_BODY;
  mainEl(h).appendChild(historic);

  const turns = api.getChatGPTTurns();
  assert.strictEqual(turns.length, 1);
  assert.strictEqual(api.turnRole(turns[0]), 'user');
});
