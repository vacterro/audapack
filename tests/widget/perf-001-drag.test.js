'use strict';

const { test } = require('node:test');
const assert = require('node:assert');
const { setup, FakeEvent } = require('./helpers');

test('PERF-001: pointer-move burst performs zero layout reads', () => {
  const { h, api } = setup();
  assert.doesNotThrow(() => api.mount());
  const panel = h.dom.getElementById('acb-popup');
  assert.ok(panel, 'panel must mount');
  const titlebar = panel.querySelector('#acb-titlebar');
  assert.ok(titlebar);

  const down = () => titlebar.dispatchEvent(
    new FakeEvent('pointerdown', { pointerId: 7, clientX: 40, clientY: 30, button: 0, isPrimary: true })
  );
  const move = (x, y) => h.window.dispatchEvent(
    new FakeEvent('pointermove', { pointerId: 7, clientX: x, clientY: y, cancelable: true })
  );
  const up = () => h.window.dispatchEvent(new FakeEvent('pointerup', { pointerId: 7 }));

  h.counters.rectReads = 0;
  down();
  assert.strictEqual(h.counters.rectReads, 1, 'drag start clamps once (one layout read)');

  h.counters.rectReads = 0;
  for (let i = 0; i < 10; i += 1) move(40 + i, 30 + i);
  assert.strictEqual(h.counters.rectReads, 0, 'raw pointer-moves must not read layout');
  assert.ok(
    h.timers.pending().some(due => due > 0 && due <= 16),
    'at most one animation frame is scheduled'
  );

  h.advance(16);
  assert.strictEqual(h.counters.rectReads, 0, 'geometry-free frame application reads no layout');
  assert.strictEqual(panel.style.getPropertyValue('left'), '25px');
  assert.strictEqual(panel.style.getPropertyValue('top'), '81px');

  for (let i = 0; i < 10; i += 1) move(50 + i, 50 + i);
  h.advance(16);
  assert.strictEqual(h.counters.rectReads, 0, 'second burst still reads no layout');
  assert.strictEqual(panel.style.getPropertyValue('left'), '35px');
  assert.strictEqual(panel.style.getPropertyValue('top'), '101px');

  h.counters.rectReads = 0;
  up();
  assert.strictEqual(h.counters.rectReads, 1, 'drag end commits the position once');
});

test('PERF-001: pointercancel ends the drag and saves the position', () => {
  const { h, api } = setup();
  api.mount();
  const panel = h.dom.getElementById('acb-popup');
  const titlebar = panel.querySelector('#acb-titlebar');

  titlebar.dispatchEvent(
    new FakeEvent('pointerdown', { pointerId: 9, clientX: 10, clientY: 10, button: 0, isPrimary: true })
  );
  h.window.dispatchEvent(new FakeEvent('pointercancel', { pointerId: 9 }));
  h.advance(16);
  assert.strictEqual(panel.style.getPropertyValue('left'), '16px', 'no frame applies after cancel');
});

test('PERF-001: move without an active drag is ignored', () => {
  const { h, api } = setup();
  api.mount();
  const before = h.timers.pending().length;
  h.window.dispatchEvent(new FakeEvent('pointermove', { pointerId: 3, clientX: 50, clientY: 50 }));
  h.advance(16);
  assert.strictEqual(h.timers.pending().length, before, 'no drag frame is scheduled without pointerdown');
});