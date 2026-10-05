'use strict';

// T-269 / SRC-110: "widget shows BUSY during managed Core generation (0.0.91)".
//
// This is the operator's own description of the failure, asserted end to end on
// the MEASURED live shape rather than on a synthetic one.
//
// The 0.0.91 live acceptance failed again, and T-267's CDP session on the
// dedicated AUDAPACK profile proved the failing layer: the 2026-10 ChatGPT build
// renamed every attribute the turn reader knew, so getChatGPTTurns() returned []
// over a conversation that plainly held the Core --
//   lineage=TURN_DISCOVERY reason=no-user-turn-discovered turns=0(u0/a0)
// With no turn there is no lineage, and with no lineage the runtime never
// adopts, so superCompactAutoLabel() has neither a wave from the DOM nor a wave
// from the runtime stage and answers BUSY over a Core that is running right now.
//
// The DOM below is copied from that measurement, not invented: the wrapper chain
// group/user-message > bg-user-message > [data-user-message-bubble], the
// [data-chatgpt-search-message-ids] message wrapper, the [data-turn-key] thread
// wrapper, the [data-conversation-role="assistant"] answer half, the current
// composer form with no data-type, the ProseMirror "Ask ChatGPT" editor, and the
// real <button type="button" aria-label="Stop"> generation control.
//
// Two things are asserted, and they are different things:
//   1. the reader sees the Core on the current build (the proven layer), and
//   2. the compact label is CORE, never BUSY, in every managed stage the Core can
//      legally be in while ChatGPT is generating.
// Ordinary non-audit generation must still read BUSY: a fix that adopted every
// conversation would be worse than the defect.

const { test } = require('node:test');
const assert = require('node:assert');
const { setup, mainEl, runtimeFixture } = require('./helpers');

const RECEIPT = 'startcore-mutcsiae-b9785';
const THREAD_KEY = '63d00465-3614-4c89-a037-c3a7b96d6bff';
const ASSISTANT_MSG_ID = 'a1910972-c34d-445c-88ef-9ee8dc99d181';
const RUN_ID = 'run-w7-016';

const CORE_BODY = [
  'AUDIT CORE - wave 1/3 of Quick 3 Waves.',
  `CAMPAIGN_RUN_ID: ${RUN_ID}`,
  'PROJECT_NAME: FastPrompter',
  `ACB_CHAIN_RECEIPT: ${RECEIPT}`
].join('\n');

// --- the measured current-build DOM -----------------------------------------

function currentBuildUserMessage(h, text) {
  const message = h.el('div', { 'data-chatgpt-search-message-ids': THREAD_KEY });
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

// [data-turn-key] spans the WHOLE exchange, so reading it as a turn would
// collapse both halves into one node.
function mountThread(h, messages) {
  const scroll = h.el('div', { 'data-app-action-timeline-scroll': '' });
  const threadKey = h.el('div', { 'data-turn-key': THREAD_KEY });
  const findTarget = h.el('div', { 'data-thread-find-target': '' });
  for (const message of messages) findTarget.appendChild(message);
  threadKey.appendChild(findTarget);
  scroll.appendChild(threadKey);
  mainEl(h).appendChild(scroll);
  return threadKey;
}

function currentComposer(h) {
  const form = h.el('form', { class: 'relative flex flex-col gap-2' });
  const input = h.el('div', {
    contenteditable: 'true',
    role: 'textbox',
    'aria-label': 'Ask ChatGPT',
    class: 'ProseMirror'
  });
  input.isContentEditable = true;
  form.appendChild(input);
  form.appendChild(h.el('div', { class: 'flex items-center' }));
  mainEl(h).appendChild(form);
  return { form, input };
}

function mountGeneratingStop(h, form) {
  const stop = h.el('button', { type: 'button', 'aria-label': 'Stop' });
  form.appendChild(stop);
  h.mutate(form);
  return stop;
}

// The live managed window: A3 armed, the Core on screen, ChatGPT generating.
function liveManagedCore(options = {}) {
  const { h, api } = setup();
  api.state.auditProfile = 'quick3';
  const { form } = currentComposer(h);
  mountGeneratingStop(h, form);
  mountThread(h, [
    currentBuildUserMessage(h, CORE_BODY),
    currentBuildAssistantMessage(h, node => { node._text = 'ARCHITECTURE'; })
  ]);
  api.autoRuntime = runtimeFixture({
    enabled: true,
    stage: options.stage || 'wait-core',
    expectedKind: options.expectedKind || 'core',
    runId: RUN_ID
  });
  return { h, api };
}

// ---------------------------------------------------------------------------
// THE PROVEN LAYER: the reader sees the Core the live page rendered
// ---------------------------------------------------------------------------

test('T-269: the current-build Core is a visible user turn, not a lost one', () => {
  const { api } = liveManagedCore();

  const turns = api.getChatGPTTurns();
  const user = turns.find(turn => api.turnRole(turn) === 'user');
  assert.ok(user,
    'the Core the live page rendered must be discoverable; turns=' +
    `${api.liveAuditLineageSnapshot().userTurnCount}`);
  assert.ok(String(user.innerText || '').includes(RECEIPT),
    'the discovered user turn is the composed Core itself');
});

test('T-269: the lineage classifier no longer reports TURN_DISCOVERY', () => {
  const { api } = liveManagedCore();

  const verdict = api.classifyLiveAuditLineage(api.liveAuditLineageSnapshot());
  assert.notStrictEqual(verdict.failure, 'TURN_DISCOVERY',
    `lineage is still lost: ${JSON.stringify(verdict)}`);
});

// ---------------------------------------------------------------------------
// THE REPORTED SYMPTOM: BUSY over a Core that is running right now
// ---------------------------------------------------------------------------

test('T-269: a managed Core never reads BUSY in any stage it can legally occupy', () => {
  // The post-SEND park (0.0.91), the adopted wave, and the wait stage that
  // AUDITING is crossed on. Each one is a Core that is genuinely in flight.
  for (const stage of ['await-core-user', 'wait-core']) {
    const { api } = liveManagedCore({ stage, expectedKind: 'core' });
    const label = api.superCompactAutoLabel();
    assert.strictEqual(label, 'CORE',
      `stage ${stage} read ${label} over a live managed Core`);
  }
});

// The strongest remaining shape: the runtime lost its own state (route
// hydration, a re-arm after reload) and sits at plain `idle` while the Core is
// visibly running. Here the DOM is the ONLY evidence there is an audit, so if
// the reader misses it there is nothing left and the cell reads BUSY. That is
// exactly what the operator saw.
test('T-269: a Core on screen still reads CORE after the runtime went blank', () => {
  const { api } = liveManagedCore({ stage: 'idle' });
  api.autoRuntime.expectedKind = '';
  api.autoRuntime.coreUserId = '';

  const label = api.superCompactAutoLabel();
  assert.strictEqual(label, 'CORE',
    `an enabled runtime at idle over a live Core read ${label}`);

  const verdict = api.classifyLiveAuditLineage(api.liveAuditLineageSnapshot());
  assert.notStrictEqual(verdict.failure, 'TURN_DISCOVERY',
    `lineage is still lost: ${JSON.stringify(verdict)}`);
});

test('T-269: the lineage tool agrees the Core is recognized, not lost', () => {
  const { api } = liveManagedCore({ stage: 'await-core-user' });

  const snapshot = api.liveAuditLineageSnapshot();
  assert.strictEqual(snapshot.liveAuditKind, 'core',
    `lineage kind: ${JSON.stringify({ liveAuditKind: snapshot.liveAuditKind })}`);
  assert.ok(snapshot.userTurnCount > 0);
});

// ---------------------------------------------------------------------------
// THE BOUNDARY: ordinary generation is still BUSY, never adopted
// ---------------------------------------------------------------------------

test('T-269: an ordinary non-audit generation still reads BUSY', () => {
  const { h, api } = setup();
  api.state.auditProfile = 'quick3';
  const { form } = currentComposer(h);
  mountGeneratingStop(h, form);
  // A human question in the chat: a real user turn, not an AUDIT CORE.
  mountThread(h, [currentBuildUserMessage(h, 'why is the sky blue?')]);
  api.autoRuntime = runtimeFixture({ enabled: true, stage: 'idle', runId: RUN_ID });

  assert.strictEqual(api.superCompactAutoLabel(), 'BUSY',
    'ordinary generation must never be adopted into an audit wave');
});
