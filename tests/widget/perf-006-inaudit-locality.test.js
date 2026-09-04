'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const { setup, assistantTurn, addTurns } = require('./helpers');

// PERF-003 (audit/1.md): INAUDIT attachment had incremental input and
// full-history processing. The MutationObserver knows which subtree changed,
// and that locality was discarded -- every relevant burst re-ran
// attachInauditActions(document), revisiting every historical assistant turn.
// Measured with the controls ALREADY attached, so each call added nothing:
// 20 turns = 162 querySelectorAll calls, 100 = 802, 300 = 2402 / 116.9 ms.

const RESPONSE_CONTROL = '[data-acb-inaudit-scope="response"]';

function stable(h, id, withBlock = false) {
  return assistantTurn(h, id, turn => {
    turn.appendChild(h.el('div', { class: 'markdown prose' }, `answer ${id}`));
    if (withBlock) {
      const wrapper = h.el('div');
      wrapper.appendChild(h.el('pre', {}, `code ${id}`));
      turn.appendChild(wrapper);
    }
    turn.appendChild(actionBar(h));
  });
}

function actionBar(h) {
  const actions = h.el('div', { 'aria-label': 'Response actions' });
  actions.appendChild(h.el('button', {
    'data-testid': 'copy-turn-action-button',
    'aria-label': 'Copy response'
  }));
  return actions;
}

/** Hydrate `count` turns, attach controls, and drain the widget's boot timers
 *  so a later `advance` measures only the attach pass under test. */
function settled(count) {
  const { h, api } = setup();
  const turns = [];
  for (let i = 0; i < count; i++) turns.push(stable(h, `t${i}`));
  addTurns(h, turns);
  h.advance(5000);
  api.attachInauditActions(h.dom);
  api.inauditDirtyTurns.clear();
  return { h, api, turns };
}

function attachDirty(h, api, records) {
  api.markInauditTurnsDirty(records);
  api.scheduleInauditActionAttach(200);
  h.advance(300);
}

test('PERF-003: one dirty turn costs one turn of work, not the whole conversation', () => {
  const { h, api } = settled(40);
  const fresh = stable(h, 'fresh');
  addTurns(h, [fresh]);

  const before = h.counters.qsa;
  attachDirty(h, api, [{ target: fresh, addedNodes: [fresh] }]);
  const localCost = h.counters.qsa - before;

  assert.ok(fresh.querySelector(RESPONSE_CONTROL), 'the new turn got its control');

  const fullBefore = h.counters.qsa;
  api.attachInauditActions(h.dom);
  const fullCost = h.counters.qsa - fullBefore;

  assert.ok(
    localCost * 4 < fullCost,
    `one dirty turn cost ${localCost} selector calls against ${fullCost} for the full scan`
  );
});

test('PERF-003: cost of adding one turn does not grow with history length', () => {
  const measure = count => {
    const { h, api } = settled(count);
    const fresh = stable(h, 'newest');
    addTurns(h, [fresh]);
    const before = h.counters.qsa;
    attachDirty(h, api, [{ target: fresh, addedNodes: [fresh] }]);
    return h.counters.qsa - before;
  };

  const small = measure(20);
  const large = measure(200);
  assert.ok(
    large <= small + 2,
    `adding one turn cost ${small} selector calls at 20 turns and ${large} at 200 -- it must stay flat`
  );
});

test('PERF-003: a rerendered action bar inside an OLD turn is repaired', () => {
  const { h, api, turns } = settled(5);
  const old = turns[1];
  assert.ok(old.querySelector(RESPONSE_CONTROL));

  // React replaces the action bar: our button goes with it.
  old.querySelector('[aria-label="Response actions"]').remove();
  const replacement = actionBar(h);
  old.appendChild(replacement);
  assert.equal(old.querySelector(RESPONSE_CONTROL), null);

  attachDirty(h, api, [{ target: replacement, addedNodes: [replacement] }]);

  assert.ok(
    old.querySelector(RESPONSE_CONTROL),
    'locality must not become a stale "already processed" flag'
  );
});

test('PERF-003: a container carrying turns marks every turn inside it', () => {
  const { h, api } = settled(3);

  // Route hydration: a wrapper appears with several turns already inside.
  const wrapper = h.el('div');
  const first = stable(h, 'hydrated-1');
  const second = stable(h, 'hydrated-2');
  wrapper.appendChild(first);
  wrapper.appendChild(second);
  addTurns(h, [wrapper]);

  attachDirty(h, api, [{ target: wrapper, addedNodes: [wrapper] }]);

  assert.ok(first.querySelector(RESPONSE_CONTROL));
  assert.ok(second.querySelector(RESPONSE_CONTROL));
});

test('PERF-003: a mutation resolving to no turn falls back to the full scan', () => {
  const { h, api } = setup();
  const turns = [];
  for (let i = 0; i < 4; i++) turns.push(stable(h, `t${i}`));
  addTurns(h, turns);
  h.advance(5000);
  // Deliberately NOT attached: a mutation that resolves to no turn must not be
  // skipped, or a conversation could stay without controls forever.
  for (const turn of turns) {
    for (const button of turn.querySelectorAll(RESPONSE_CONTROL)) button.remove();
  }
  api.inauditDirtyTurns.clear();

  const foreign = h.el('div');
  addTurns(h, [foreign]);
  api.markInauditTurnsDirty([{ target: foreign, addedNodes: [] }]);
  api.scheduleInauditActionAttach(200, { fullScan: true });
  h.advance(300);

  for (const turn of turns) {
    assert.ok(turn.querySelector(RESPONSE_CONTROL), 'full-scan fallback missed a turn');
  }
});

test('PERF-003: our own button mutations are still ignored', () => {
  const { h, api, turns } = settled(3);
  // The class attribute is what the guard reads, and it is what the real
  // control carries; the harness does not mirror `className` into classList,
  // so the fixture sets it directly.
  const ours = h.el('button', { class: 'acb-inaudit-action', 'data-acb-inaudit-scope': 'response' });
  turns[0].appendChild(ours);

  const { dirty } = api.markInauditTurnsDirty([{ target: ours, addedNodes: [] }]);
  assert.equal(dirty, 0, 'a mutation inside our own control must never schedule work');
});
