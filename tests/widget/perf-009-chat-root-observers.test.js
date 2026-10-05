'use strict';

// PERF-003 (audit/9.md): the widget guardian owns re-anchoring EVERY observer
// bound to the ChatGPT chat root. Before this fix it repaired only the Auto
// Audit observer, so a ChatGPT SPA <main> replacement permanently orphaned the
// INAUDIT capture observer and new assistant turns stopped receiving IA
// controls while the widget itself looked healthy.

const test = require('node:test');
const assert = require('node:assert/strict');
const { setup, mainEl, assistantTurn } = require('./helpers');

function stableAssistant(h, id) {
  return assistantTurn(h, id, turn => {
    turn.appendChild(h.el('div', { class: 'markdown prose' }, 'finished answer'));
    const actions = h.el('div', { 'aria-label': 'Response actions' });
    actions.appendChild(h.el('button', {
      'data-testid': 'copy-turn-action-button',
      'aria-label': 'Copy response'
    }));
    turn.appendChild(actions);
  });
}

function observersOn(h, root) {
  return Array.from(h._observers).filter(observer => observer.connected && observer.root === root);
}

function inauditObserversOn(h, api, root) {
  return observersOn(h, root).filter(observer => observer === api.inauditCaptureObserver);
}

function autoObserversOn(h, api, root) {
  return observersOn(h, root).filter(observer => observer === api.autoAuditObserver);
}

function bootGuardian(h, api) {
  api.ensureWidgetConnected();
  api.installWidgetGuardian();
  api.ensureInauditCaptureObserver();
}

function replaceMain(h) {
  const oldMain = mainEl(h);
  const newMain = h.el('main');
  oldMain.remove();
  h.dom.body.appendChild(newMain);
  return { oldMain, newMain };
}

function runGuardian(h, api) {
  const guardian = api.widgetGuardianObserver;
  assert.ok(guardian, 'the guardian observer must exist');
  guardian.callback([], guardian);
}

test('PERF-003 (audit/9.md): the INAUDIT observer binds to the live main root', () => {
  const { h, api } = setup();
  const main = mainEl(h);
  bootGuardian(h, api);
  assert.equal(inauditObserversOn(h, api, main).length, 1);
  api.ensureInauditCaptureObserver();
  assert.equal(inauditObserversOn(h, api, main).length, 1, 'the same root must not stack observers');
});

test('PERF-003 (audit/9.md): the guardian re-anchors the INAUDIT observer after <main> replacement', () => {
  const { h, api } = setup();
  bootGuardian(h, api);
  const { oldMain, newMain } = replaceMain(h);

  // The defect: without the guardian repair the observer stays on the old root.
  runGuardian(h, api);

  assert.equal(observersOn(h, oldMain).length, 0, 'the detached root must carry no observer');
  assert.equal(inauditObserversOn(h, api, newMain).length, 1, 'exactly one INAUDIT observer on the new root');
  assert.equal(api.inauditCaptureObserverRoot, newMain);
});

test('PERF-003 (audit/9.md): a stable assistant turn in the replacement root receives IA controls', async () => {
  const { h, api } = setup();
  bootGuardian(h, api);
  replaceMain(h);
  runGuardian(h, api);

  const turn = stableAssistant(h, 'after-swap');
  mainEl(h).appendChild(turn);
  // The rebind schedules its one-time full attach pass.
  h.advance(500);
  await h.settle();

  assert.ok(
    turn.querySelector('[data-acb-inaudit-scope="response"]'),
    'the replacement root must gain IA controls without a manual ensure call'
  );
});

test('PERF-003 (audit/9.md): repeated guardian runs never multiply observers', () => {
  const { h, api } = setup();
  bootGuardian(h, api);
  const { newMain } = replaceMain(h);

  for (let i = 0; i < 5; i++) runGuardian(h, api);

  assert.equal(inauditObserversOn(h, api, newMain).length, 1);
  assert.equal(
    Array.from(h._observers).filter(o => o === api.inauditCaptureObserver && o.connected).length,
    1,
    'at most one INAUDIT observer may exist at any time'
  );
});

test('PERF-003 (audit/9.md): repeated root replacement keeps exactly one INAUDIT observer', () => {
  const { h, api } = setup();
  bootGuardian(h, api);

  let previous = mainEl(h);
  for (let i = 0; i < 4; i++) {
    const { oldMain, newMain } = replaceMain(h);
    assert.equal(oldMain, previous);
    runGuardian(h, api);
    assert.equal(observersOn(h, oldMain).length, 0, 'each detached root is disconnected');
    assert.equal(inauditObserversOn(h, api, newMain).length, 1);
    previous = newMain;
  }
});

test('PERF-003 (audit/9.md): ensureChatRootObservers reconciles both owners at once', () => {
  const { h, api } = setup();
  api.ensureWidgetConnected();
  api.startAutoAuditMonitor();
  api.ensureInauditCaptureObserver();
  const oldMain = mainEl(h);
  assert.equal(autoObserversOn(h, api, oldMain).length, 1);
  assert.equal(inauditObserversOn(h, api, oldMain).length, 1);

  const { newMain } = replaceMain(h);
  api.ensureChatRootObservers();

  assert.equal(observersOn(h, oldMain).length, 0);
  assert.equal(autoObserversOn(h, api, newMain).length, 1, 'Auto Audit observer stays a single live observer');
  assert.equal(inauditObserversOn(h, api, newMain).length, 1, 'INAUDIT observer stays a single live observer');
});

test('PERF-003 (audit/9.md): a mutation on the detached old root schedules no INAUDIT work', async () => {
  const { h, api } = setup();
  bootGuardian(h, api);
  const { oldMain } = replaceMain(h);
  runGuardian(h, api);

  h.advance(500);
  await h.settle();
  const before = h.timers.pending().length;
  h.mutate(oldMain);
  assert.equal(h.timers.pending().length, before, 'a detached root must not schedule attach work');
});
