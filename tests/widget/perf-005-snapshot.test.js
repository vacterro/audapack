'use strict';

const { test } = require('node:test');
const assert = require('node:assert');
const { setup, assistantTurn, addTurns } = require('./helpers');

function surfacedAssistant(h, surfaceCount) {
  return assistantTurn(h, 'a1', el => {
    const message = h.el('div', { 'data-message-author-role': 'assistant' });
    for (let i = 0; i < surfaceCount; i += 1) {
      const surface = h.el('div', { class: 'markdown prose' });
      surface._text = `surface ${i} answer`;
      message.appendChild(surface);
    }
    const chrome = h.el('div', { class: 'meta' });
    chrome._text = 'chrome outside surfaces';
    message.appendChild(chrome);
    const copy = h.el('button', { 'data-testid': 'copy-turn-action-button', 'aria-label': 'Copy response' });
    message.appendChild(copy);
    el.appendChild(message);
  });
}

test('PERF-005: snapshot extracts candidates, whole evidence and fingerprint in one pass', () => {
  const { h, api } = setup();
  const turn = surfacedAssistant(h, 3);
  addTurns(h, [turn]);

  h.counters.innerTextReads = 0;
  const snapshot = api.buildAssistantSnapshot(turn);
  assert.strictEqual(snapshot.sourceCount, 4);
  assert.deepStrictEqual(
    Array.from(snapshot.candidates),
    [
      'surface 0 answer',
      'surface 1 answer',
      'surface 2 answer',
      'surface 0 answersurface 1 answersurface 2 answerchrome outside surfaces'
    ]
  );
  assert.strictEqual(
    Array.from(snapshot.whole)[0],
    'surface 0 answersurface 1 answersurface 2 answerchrome outside surfaces'
  );
  assert.match(snapshot.fingerprint, /^\d+:[0-9a-z]+$/);
  assert.ok(h.counters.innerTextReads <= 7, `three surfaces + message + turn, each read at most once (got ${h.counters.innerTextReads})`);
});

test('PERF-005: candidate collection is bounded at 16 with early exit', () => {
  const { h, api } = setup();
  const turn = surfacedAssistant(h, 20);
  addTurns(h, [turn]);

  h.counters.innerTextReads = 0;
  const snapshot = api.buildAssistantSnapshot(turn);
  assert.strictEqual(snapshot.candidates.length, 16, 'candidates must be capped');
  assert.strictEqual(snapshot.sourceCount, 16);
  assert.ok(h.counters.innerTextReads <= 18, `sixteen surface reads plus message and turn (got ${h.counters.innerTextReads})`);
});

test('PERF-005: duplicate surfaces are collected once', () => {
  const { h, api } = setup();
  const turn = assistantTurn(h, 'a2', el => {
    const message = h.el('div', { 'data-message-author-role': 'assistant' });
    const a = h.el('div', { class: 'markdown prose' });
    a._text = 'same answer';
    const b = h.el('pre');
    b._text = 'same answer';
    message.appendChild(a);
    message.appendChild(b);
    el.appendChild(message);
  });
  addTurns(h, [turn]);
  const snapshot = api.buildAssistantSnapshot(turn);
  assert.strictEqual(snapshot.candidates.length, 2);
  assert.strictEqual(snapshot.candidates[0], 'same answer');
});

test('PERF-005: completedAssistantCandidate extracts once and fingerprints stably', () => {
  const { h, api } = setup();
  const turn = surfacedAssistant(h, 3);
  addTurns(h, [turn]);
  api.autoRuntime.enabled = true;
  api.autoRuntime.stage = 'wait-core';

  h.counters.innerTextReads = 0;
  const first = api.completedAssistantCandidate(turn);
  assert.ok(h.counters.innerTextReads <= 7, `gate, fallback and fingerprint share one extraction (got ${h.counters.innerTextReads})`);
  assert.strictEqual(first.complete, false, 'first evaluation arms the stabilization window');
  const armedKey = api.autoRuntime.stableResponseKey;
  assert.ok(armedKey, 'candidate arms a fingerprint key');

  const second = api.completedAssistantCandidate(turn);
  assert.strictEqual(api.autoRuntime.stableResponseKey, armedKey, 'identical answer must fingerprint identically');

  h.counters.innerTextReads = 0;
  api.completedAssistantCandidate(turn);
  assert.ok(h.counters.innerTextReads <= 7, `re-evaluation still extracts once (got ${h.counters.innerTextReads})`);

  const message = turn.querySelector('[data-message-author-role="assistant"]');
  message.querySelector('.markdown')._text = 'surface 0 answer CHANGED';
  api.completedAssistantCandidate(turn);
  assert.notStrictEqual(api.autoRuntime.stableResponseKey, armedKey, 'changed answer must invalidate the fingerprint');
});

test('PERF-005: completedAssistantCandidate respects the extraction cap too', () => {
  const { h, api } = setup();
  const turn = surfacedAssistant(h, 25);
  addTurns(h, [turn]);
  api.autoRuntime.enabled = true;
  api.autoRuntime.stage = 'wait-core';

  h.counters.innerTextReads = 0;
  api.completedAssistantCandidate(turn);
  assert.ok(h.counters.innerTextReads <= 18, `candidate cap applies inside the whole evaluation (got ${h.counters.innerTextReads})`);
  assert.ok(api.autoRuntime.stableResponseKey);
});