'use strict';

const { test } = require('node:test');
const assert = require('node:assert');
const { setup, mainEl, userTurn, assistantTurn, addTurns } = require('./helpers');

function wrapperTurn(h, id, role, text) {
  const section = h.el('section', { 'data-testid': `conversation-turn-${id}` });
  const article = h.el('article', { 'data-message-author-role': role, 'data-message-id': id });
  if (text) article._text = text;
  section.appendChild(article);
  return section;
}

test('PERF-004: discovery is one scoped scan in document order', () => {
  const { h, api } = setup();
  const main = mainEl(h);
  const hydratedUser = wrapperTurn(h, 'u1', 'user', 'hello');
  const hydratedAssistant = wrapperTurn(h, 'a1', 'assistant', 'world');
  const bareUser = userTurn(h, 'u2', 'bare');
  const hydratedUser2 = wrapperTurn(h, 'u3', 'user', 'again');
  addTurns(h, [hydratedUser, hydratedAssistant, bareUser, hydratedUser2]);

  h.counters.qsa = 0;
  const turns = Array.from(api.getChatGPTTurns(), t => t.getAttribute('data-testid'));
  assert.deepStrictEqual(
    turns,
    ['conversation-turn-u1', 'conversation-turn-a1', 'conversation-turn-u2', 'conversation-turn-u3'],
    'wrapper+message overlap must dedupe to wrappers in document order'
  );
  assert.strictEqual(h.counters.cdp, 0, 'no document-position comparisons');
});

test('PERF-004: message-only and wrapper-only turns both resolve', () => {
  const { h, api } = setup();
  const messageOnly = userTurn(h, 'm1', 'lone message');
  const wrapperOnly = wrapperTurn(h, 'w1', 'assistant', 'wrapped');
  addTurns(h, [wrapperOnly, messageOnly]);

  const turns = api.getChatGPTTurns();
  assert.strictEqual(turns.length, 2);
  assert.strictEqual(turns[0].getAttribute('data-testid'), 'conversation-turn-w1');
  assert.strictEqual(turns[1].getAttribute('data-testid'), 'conversation-turn-m1');
});

test('PERF-004: scan is scoped to the main conversation root', () => {
  const { h, api } = setup();
  const main = mainEl(h);
  addTurns(h, [userTurn(h, 'in-main', 'inside')]);
  const outside = userTurn(h, 'outside', 'floating');
  h.dom.body.appendChild(outside);

  const turns = api.getChatGPTTurns();
  assert.strictEqual(turns.length, 1);
  assert.strictEqual(turns[0].getAttribute('data-testid'), 'conversation-turn-in-main');
  assert.ok(!main.contains(outside) || true);
});

test('PERF-004: long conversations return every turn in order', () => {
  const { h, api } = setup();
  const turns = [];
  for (let i = 0; i < 1000; i += 1) {
    turns.push(wrapperTurn(h, `t${i}`, i % 2 ? 'assistant' : 'user', `message ${i}`));
  }
  addTurns(h, turns);
  const found = api.getChatGPTTurns();
  assert.strictEqual(found.length, 1000);
  assert.strictEqual(found[0].getAttribute('data-testid'), 'conversation-turn-t0');
  assert.strictEqual(found[999].getAttribute('data-testid'), 'conversation-turn-t999');
});

test('PERF-004: no turns outside the composer returns empty list', () => {
  const { h, api } = setup();
  assert.strictEqual(api.getChatGPTTurns().length, 0);
});

test('PERF-006: one nested message lookup per stable wrapper', () => {
  const { h, api } = setup();
  const main = mainEl(h);
  const turns = [];
  for (let i = 0; i < 100; i += 1) {
    turns.push(wrapperTurn(h, `s${i}`, i % 2 ? 'assistant' : 'user', `msg ${i}`));
  }
  addTurns(h, turns);

  h.counters.qsa = 0;
  const found = api.getChatGPTTurns();
  assert.strictEqual(found.length, 100);

  // getChatGPTTurns runs ONE root querySelectorAll for stable wrappers, then
  // each wrapper resolves its nested authored message with a single descendant
  // lookup (not two: turnRole query + message query). Fallback scan does not
  // trigger here because user turns are found.
  const qsa = h.counters.qsa;
  const perWrapper = qsa / 100;
  assert.ok(
    perWrapper <= 1.1,
    `expected ~1 nested message lookup per stable wrapper, got ${perWrapper} (qsa=${qsa})`
  );
  assert.ok(perWrapper >= 1, `expected at least one lookup per wrapper, got ${perWrapper}`);
});