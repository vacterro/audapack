'use strict';

const { test } = require('node:test');
const assert = require('node:assert');
const { setup, mainEl, runtimeFixture } = require('./helpers');

function observersFor(h, api, root) {
  return Array.from(h._observers).filter(o => o === api.autoAuditObserver && o.connected && o.root === root);
}

test('PERF-002: disabled state observes navigation only, no character data', () => {
  const { h, api } = setup();
  const main = mainEl(h);
  api.startAutoAuditMonitor();
  const observers = observersFor(h, api, main);
  assert.strictEqual(observers.length, 1);
  assert.strictEqual(observers[0].options.characterData, false, 'no text-content tracking while disabled');
  assert.strictEqual(api.autoAuditObserverConfig(), 'nav');
});

test('PERF-002: wait-core stage streams character data', () => {
  const { h, api } = setup();
  const main = mainEl(h);
  api.autoRuntime.enabled = true;
  api.autoRuntime.stage = 'wait-core';
  api.startAutoAuditMonitor();
  const observers = observersFor(h, api, main);
  assert.strictEqual(observers.length, 1);
  assert.strictEqual(observers[0].options.characterData, true, 'streaming stages track text');
  assert.strictEqual(api.autoAuditObserverConfig(), 'stream');
});

test('PERF-002: paused stage reverts to turns-only observer', () => {
  const { h, api } = setup();
  const main = mainEl(h);
  api.autoRuntime.enabled = true;
  api.autoRuntime.stage = 'wait-core';
  api.startAutoAuditMonitor();
  api.pauseAutoAudit('test');
  const observers = observersFor(h, api, main);
  assert.strictEqual(observers.length, 1);
  assert.strictEqual(observers[0].options.characterData, false, 'pause must downgrade text tracking');
  assert.strictEqual(api.autoAuditObserverConfig(), 'turns');
});

test('PERF-002: disabling automation rebinds the observer', () => {
  const { h, api } = setup();
  const main = mainEl(h);
  api.autoRuntime.enabled = true;
  api.autoRuntime.stage = 'wait-core';
  api.startAutoAuditMonitor();
  assert.strictEqual(observersFor(h, api, main)[0].options.characterData, true);
  api.setAutoAuditEnabled(false);
  const observers = observersFor(h, api, main);
  assert.strictEqual(observers.length, 1);
  assert.strictEqual(observers[0].options.characterData, false, 'disable must rebind to navigation config');
  assert.strictEqual(api.autoAuditObserverConfig(), 'nav');
});

test('PERF-002: mutations schedule checks only while enabled', () => {
  const { h, api } = setup();
  const main = mainEl(h);
  api.autoRuntime.enabled = false;
  api.startAutoAuditMonitor();
  h.advance(0);
  const pendingBeforeMutation = h.timers.pending().length;
  h.mutate(main);
  assert.strictEqual(h.timers.pending().length, pendingBeforeMutation, 'disabled observer must not schedule checks');

  api.autoRuntime.enabled = true;
  api.autoRuntime.stage = 'wait-core';
  api.ensureAutoAuditObserver();
  h.mutate(main);
  assert.ok(h.timers.pending().length > 0, 'enabled observer must schedule a debounced check');
});

test('PERF-002: stage transition inside a stream window keeps one observer', () => {
  const { h, api } = setup();
  const main = mainEl(h);
  api.autoRuntime.enabled = true;
  api.autoRuntime.stage = 'wait-core';
  api.startAutoAuditMonitor();
  const before = observersFor(h, api, main);
  api.autoRuntime.stage = 'wait-performance';
  api.ensureAutoAuditObserver();
  const after = observersFor(h, api, main);
  assert.strictEqual(after.length, 1, 'stream-to-stream transition must not stack observers');
  assert.strictEqual(after[0], before[0], 'same config must keep the same observer instance');
});
