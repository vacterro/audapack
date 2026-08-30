'use strict';

const { test } = require('node:test');
const assert = require('node:assert');
const { setup, userTurn, addTurns, leaseFor } = require('./helpers');

test('W2-004: canonical markers classify', () => {
  const { api } = setup();
  assert.strictEqual(api.classifyAuditMessage('AUDIT CORE — run the full sweep'), 'core');
  assert.strictEqual(api.classifyAuditMessage('AUDIT SECOND WAVE — narrow the list'), 'second');
  assert.strictEqual(api.classifyAuditMessage('AUDIT PERFORMANCE — measure everything'), 'performance');
  assert.strictEqual(api.classifyAuditMessage('AUDIT PERFORMANCE/STABILITY/EFFECTIVENESS — deep check'), 'performance');
});

test('W2-004: CONTINUE framing classifies', () => {
  const { api } = setup();
  assert.strictEqual(api.classifyAuditMessage('AUDIT CORE CONTINUE — keep going'), 'core');
  assert.strictEqual(api.classifyAuditMessage('AUDIT SECOND WAVE CONTINUE — more'), 'second');
  assert.strictEqual(api.classifyAuditMessage('AUDIT PERFORMANCE CONTINUE — more'), 'performance');
});

test('W2-004: em/en/hyphen dash framing classifies', () => {
  const { api } = setup();
  assert.strictEqual(api.classifyAuditMessage('AUDIT CORE — full sweep'), 'core');
  assert.strictEqual(api.classifyAuditMessage('AUDIT CORE – full sweep'), 'core');
  assert.strictEqual(api.classifyAuditMessage('AUDIT CORE - full sweep'), 'core');
  assert.strictEqual(api.classifyAuditMessage('AUDIT SECOND WAVE - more'), 'second');
  assert.strictEqual(api.classifyAuditMessage('AUDIT CORE—full sweep'), 'core');
});

test('W2-004: marker must be first meaningful authored line', () => {
  const { api } = setup();
  assert.strictEqual(api.classifyAuditMessage('please run AUDIT CORE on this'), '');
  assert.strictEqual(api.classifyAuditMessage('I want to start\nAUDIT CORE — now'), '');
  assert.strictEqual(api.classifyAuditMessage('---\nAUDIT CORE — now'), '');
  assert.strictEqual(api.classifyAuditMessage('Some context: "AUDIT CORE" is required'), '');
  assert.strictEqual(api.classifyAuditMessage(''), '');
  assert.strictEqual(api.classifyAuditMessage('   '), '');
});

test('W2-004: structural lines never classify', () => {
  const { api } = setup();
  assert.strictEqual(api.classifyAuditMessage('> AUDIT CORE — quoted'), '');
  assert.strictEqual(api.classifyAuditMessage('# AUDIT CORE'), '');
  assert.strictEqual(api.classifyAuditMessage('| AUDIT CORE |'), '');
  assert.strictEqual(api.classifyAuditMessage('`AUDIT CORE`'), '');
  assert.strictEqual(api.classifyAuditMessage('- AUDIT CORE\n- and more'), '');
  assert.strictEqual(api.classifyAuditMessage('* AUDIT CORE\n* and more'), '');
});

test('W2-004: quoted/fenced marker only with canonical body framing', () => {
  const { api } = setup();
  assert.strictEqual(api.classifyAuditMessage('"AUDIT CORE" is the goal'), '');
  assert.strictEqual(api.classifyAuditMessage('```\nAUDIT CORE — demo\n```'), '');
});

test('W2-004: classifyAuditTurn only for user turns', () => {
  const { h, api } = setup();
  const assistant = h.el('article', {
    'data-message-author-role': 'assistant',
    'data-testid': 'conversation-turn-a1',
    'data-message-id': 'a1'
  });
  assistant._text = 'AUDIT CORE — done';
  addTurns(h, [assistant]);
  assert.strictEqual(api.classifyAuditTurn(assistant), '');
});

test('W2-004: big-paste label classifies only with command framing', () => {
  const { h, api } = setup();
  const turn = userTurn(h, 'u1');
  const group = h.el('div', { role: 'group', 'aria-label': 'AUDIT SECOND WAVE — long pasted text' });
  group.appendChild(h.el('button', { name: 'expand-file-tile', 'aria-label': 'Show in text field' }));
  turn.appendChild(group);
  addTurns(h, [turn]);
  assert.strictEqual(api.classifyAuditTurn(turn), 'second');
});

test('W2-004: bare attachment filename never classifies', () => {
  const { h, api } = setup();
  const turn = userTurn(h, 'u1');
  const group = h.el('div', { role: 'group', 'aria-label': 'audit_second_wave_notes.md' });
  group.appendChild(h.el('button', { name: 'expand-file-tile', 'aria-label': 'Show in text field' }));
  turn.appendChild(group);
  addTurns(h, [turn]);
  assert.strictEqual(api.classifyAuditTurn(turn), '');
});

test('W2-004: canonical command label classifies even when ChatGPT omits expand control', () => {
  const { h, api } = setup();
  const turn = userTurn(h, 'u1');
  const group = h.el('div', { role: 'group', 'aria-label': 'AUDIT CORE — text' });
  turn.appendChild(group);
  addTurns(h, [turn]);
  assert.strictEqual(api.classifyAuditTurn(turn), 'core');
});

test('W2-004: turn text is the canonical source', () => {
  const { h, api } = setup();
  const turn = userTurn(h, 'u1', 'AUDIT PERFORMANCE — measure');
  addTurns(h, [turn]);
  assert.strictEqual(api.classifyAuditTurn(turn), 'performance');
});
