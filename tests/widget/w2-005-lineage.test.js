'use strict';

const { test } = require('node:test');
const assert = require('node:assert');
const { setup, userTurn, assistantTurn, addTurns } = require('./helpers');

test('W2-005: plain user message between core and second interrupts lineage', () => {
  const { h, api } = setup();
  const core = userTurn(h, 'c1', 'AUDIT CORE — sweep');
  const interruption = userTurn(h, 'u-int', 'how are you?');
  const second = userTurn(h, 's1', 'AUDIT SECOND WAVE — narrow');
  addTurns(h, [core, interruption, second]);
  assert.strictEqual(api.previousAuditUserTurn(second, 'core'), null);
  assert.strictEqual(api.previousAuditUserTurn(second, 'second'), null);
});

test('W2-005: sibling chain turns are pass-through', () => {
  const { h, api } = setup();
  const core = userTurn(h, 'c1', 'AUDIT CORE — sweep');
  const second1 = userTurn(h, 's1', 'AUDIT SECOND WAVE — narrow');
  const second2 = userTurn(h, 's2', 'AUDIT SECOND WAVE — narrow again');
  addTurns(h, [core, second1, second2]);
  assert.strictEqual(api.previousAuditUserTurn(second2, 'second'), second1);
  assert.strictEqual(api.previousAuditUserTurn(second2, 'core'), null);
});

test('W2-005: full core -> second -> performance lineage resolves', () => {
  const { h, api } = setup();
  const core = userTurn(h, 'c1', 'AUDIT CORE — sweep');
  const second = userTurn(h, 's1', 'AUDIT SECOND WAVE — narrow');
  const performance = userTurn(h, 'p1', 'AUDIT PERFORMANCE — measure');
  addTurns(h, [core, second, performance]);
  assert.strictEqual(api.previousAuditUserTurn(performance, 'second'), second);
  assert.strictEqual(api.previousAuditUserTurn(second, 'core'), core);
});

test('W2-005: performance siblings pass through, other kinds interrupt', () => {
  const { h, api } = setup();
  const core = userTurn(h, 'c1', 'AUDIT CORE — sweep');
  const second = userTurn(h, 's1', 'AUDIT SECOND WAVE — narrow');
  const p1 = userTurn(h, 'p1', 'AUDIT PERFORMANCE — measure');
  const p2 = userTurn(h, 'p2', 'AUDIT PERFORMANCE — measure again');
  addTurns(h, [core, second, p1, p2]);
  assert.strictEqual(api.previousAuditUserTurn(p2, 'performance'), p1);
  assert.strictEqual(api.previousAuditUserTurn(p2, 'second'), null);
});

test('W2-005: core siblings are pass-through for core searches', () => {
  const { h, api } = setup();
  const core1 = userTurn(h, 'c1', 'AUDIT CORE — sweep');
  const core2 = userTurn(h, 'c2', 'AUDIT CORE — sweep again');
  const second = userTurn(h, 's1', 'AUDIT SECOND WAVE — narrow');
  addTurns(h, [core1, core2, second]);
  assert.strictEqual(api.previousAuditUserTurn(second, 'core'), core2);
});

test('W2-005: assistant turns never interrupt lineage', () => {
  const { h, api } = setup();
  const core = userTurn(h, 'c1', 'AUDIT CORE — sweep');
  const assistant = assistantTurn(h, 'a1', el => {
    const markdown = h.el('div', { class: 'markdown' });
    markdown._text = 'The complete report for CORE is ready.';
    el.appendChild(markdown);
  });
  const second = userTurn(h, 's1', 'AUDIT SECOND WAVE — narrow');
  addTurns(h, [core, assistant, second]);
  assert.strictEqual(api.previousAuditUserTurn(second, 'core'), core);
});

test('W2-005: standalone turn without lineage returns null', () => {
  const { h, api } = setup();
  const second = userTurn(h, 's1', 'AUDIT SECOND WAVE — orphan');
  addTurns(h, [second]);
  assert.strictEqual(api.previousAuditUserTurn(second, 'core'), null);
  assert.strictEqual(api.previousAuditUserTurn(second, 'second'), null);
});

test('W2-005: resumeRuntimeFromAuditTurn rejects interrupted lineage', () => {
  const { h, api } = setup();
  const core = userTurn(h, 'c1', 'AUDIT CORE — sweep');
  const interruption = userTurn(h, 'u-int', 'where did we leave off?');
  const second = userTurn(h, 's1', 'AUDIT SECOND WAVE — narrow');
  addTurns(h, [core, interruption, second]);
  api.autoRuntime.enabled = true;
  assert.strictEqual(api.resumeRuntimeFromAuditTurn(second), false);
});

test('W2-005: resumeRuntimeFromAuditTurn accepts unbroken lineage', () => {
  const { h, api } = setup();
  const core = userTurn(h, 'c1', 'AUDIT CORE — sweep');
  const second = userTurn(h, 's1', 'AUDIT SECOND WAVE — narrow');
  addTurns(h, [core, second]);
  api.autoRuntime.enabled = true;
  assert.strictEqual(api.resumeRuntimeFromAuditTurn(second), true);
  assert.strictEqual(api.autoRuntime.coreUserId, 'c1');
  assert.strictEqual(api.autoRuntime.secondUserId, 's1');
});