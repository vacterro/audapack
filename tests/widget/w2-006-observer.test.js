'use strict';

const { test } = require('node:test');
const assert = require('node:assert');
const { setup, mainEl } = require('./helpers');

function observersFor(h, root) {
  return Array.from(h._observers).filter(o => o.connected && o.root === root);
}

test('W2-006: monitor binds one observer to the main root', () => {
  const { h, api } = setup();
  const main = mainEl(h);
  api.startAutoAuditMonitor();
  assert.strictEqual(observersFor(h, main).length, 1);
  api.startAutoAuditMonitor();
  assert.strictEqual(observersFor(h, main).length, 1, 'second call must not stack observers');
});

test('W2-006: stop disconnects the observer', () => {
  const { h, api } = setup();
  const main = mainEl(h);
  api.startAutoAuditMonitor();
  api.stopAutoAuditMonitor();
  assert.strictEqual(observersFor(h, main).length, 0);
});

test('W2-006: observer rebinds when the root is replaced', () => {
  const { h, api } = setup();
  const oldMain = mainEl(h);
  api.startAutoAuditMonitor();
  assert.strictEqual(observersFor(h, oldMain).length, 1);

  const newMain = h.el('main');
  oldMain.remove();
  h.dom.body.appendChild(newMain);
  assert.notStrictEqual(mainEl(h), oldMain);

  api.ensureAutoAuditObserver();
  assert.strictEqual(observersFor(h, oldMain).length, 0, 'old root must be detached');
  assert.strictEqual(observersFor(h, newMain).length, 1, 'new root must be observed');
});

test('W2-006: ensureAutoAuditObserver is a no-op while the same root is connected', () => {
  const { h, api } = setup();
  const main = mainEl(h);
  api.startAutoAuditMonitor();
  api.ensureAutoAuditObserver();
  assert.strictEqual(observersFor(h, main).length, 1);
});

test('W2-006: mutation on the observed root triggers a chain check', async () => {
  const { h, api } = setup();
  const main = mainEl(h);
  api.autoRuntime.enabled = true;
  api.startAutoAuditMonitor();
  h.advance(0);
  const before = h.timers.pending().length;
  h.mutate(main);
  assert.ok(h.timers.pending().length > 0, 'mutation must schedule a check');
});

test('W2-006: mutation callback re-anchors after root replacement', () => {
  const { h, api } = setup();
  const oldMain = mainEl(h);
  api.startAutoAuditMonitor();
  const newMain = h.el('main');
  oldMain.remove();
  h.dom.body.appendChild(newMain);
  h.mutate(oldMain);
  assert.strictEqual(observersFor(h, newMain).length, 1, 'callback must rebind the observer');
});

test('W2-006: one evaluation after a re-bind', () => {
  const { h, api } = setup();
  const oldMain = mainEl(h);
  api.autoRuntime.enabled = true;
  api.startAutoAuditMonitor();
  h.advance(0);
  const newMain = h.el('main');
  oldMain.remove();
  h.dom.body.appendChild(newMain);
  api.ensureAutoAuditObserver();
  assert.strictEqual(observersFor(h, newMain).length, 1);
});