'use strict';

const { test } = require('node:test');
const assert = require('node:assert');
const { setup } = require('./helpers');

function ticket(num, path) {
  const n = String(num).padStart(3, '0');
  return [
    `[P0] [CORE-${n}] ${path}`,
    `EVIDENCE: ${path} lines ${num}-${num + 5}: observed behavior`,
    `DEFECT: ${path} misbehaves when ${num} is large`,
    `REPAIR: guard ${path} with a bound`,
    `VERIFY: ${path} handles ${num} correctly under test`,
    ''
  ].join('\n');
}

function headerBody({ statusLine = 'STATUS: AUDIT_CORE: COMPLETE', tickets = 27, includeWave = true, includeHandoff = true } = {}) {
  const lines = [
    'PROJECT_NAME: testproj',
    'DATE_TIME: 2026-08-28 03:00',
    'CAMPAIGN_PROFILE: quick3',
    'CAMPAIGN_PROFILE_VERSION: 1.0.0',
    'WAVE_ID: core',
    'WAVE_INDEX: 1',
    'WAVE_COUNT: 3'
  ];
  if (includeWave) lines.push('WAVE: AUDIT CORE');
  lines.push(
    'TARGET: V:/testproj',
    'BASELINE: HEAD@abc123',
    'TEST_STATUS: TEST_PASSED',
    'VERIFIED_INSTEAD: NONE'
  );
  lines.push(statusLine);
  lines.push(`TICKETS: ${tickets}`);
  if (includeHandoff) lines.push('HANDOFF: IMPLEMENTATION_AGENT');
  lines.push(
    'COVERAGE_INSPECTED: a.py b.py c.py',
    '',
    ''
  );
  const ticketsBlock = [];
  for (let i = 1; i <= tickets; i += 1) {
    ticketsBlock.push(ticket(i, `src/mod_${i}.py`));
  }
  ticketsBlock.push('CORE_DONE_WHEN: all defects fixed and re-tested');
  return lines.join('\n') + '\n' + ticketsBlock.join('\n');
}

test('gate: well-formed COMPLETE body classifies complete', () => {
  const { api } = setup();
  const body = headerBody();
  const state = api.responseGate('wait-core', body);
  assert.strictEqual(state, 'complete', `expected complete, got ${state}`);
});

test('gate: STATUS/TICKETS glued to neighbors still classifies complete', () => {
  const { api } = setup();
  const body = headerBody({ statusLine: 'verification against current live filesSTATUS: AUDIT_CORE: COMPLETETICKETS: 27' });
  const state = api.responseGate('wait-core', body);
  assert.strictEqual(state, 'complete', `expected complete, got ${state}`);
});

test('gate: missing WAVE line still classifies complete via explicit status', () => {
  const { api } = setup();
  const body = headerBody({ includeWave: false });
  const state = api.responseGate('wait-core', body);
  assert.strictEqual(state, 'complete', `expected complete, got ${state}`);
});

test('gate: missing HANDOFF line still classifies complete', () => {
  const { api } = setup();
  const body = headerBody({ includeHandoff: false });
  const state = api.responseGate('wait-core', body);
  assert.strictEqual(state, 'complete', `expected complete, got ${state}`);
});

test('gate: ticket count mismatch degrades to partial, never unknown', () => {
  const { api } = setup();
  const body = headerBody({ tickets: 27 });
  const truncated = body.replace(ticket(27, 'src/mod_27.py'), '');
  const state = api.responseGate('wait-core', truncated);
  assert.notStrictEqual(state, 'unknown', `must not be unknown, got ${state}`);
});

test('gate: short-body assistant replies about an already-finished wave are NOT complete', () => {
  const { api } = setup();
  const body = 'The audit is already complete with 27 tickets. Nothing further to inspect.';
  const state = api.responseGate('wait-core', body);
  assert.strictEqual(state, 'unknown');
});

test('gate: response to a CONTINUE nudge (await-continuation-user) must classify COMPLETE', () => {
  const { api } = setup();
  const body = headerBody();
  const state = api.responseGate('await-continuation-user', body);
  assert.strictEqual(state, 'complete', `expected complete, got ${state}`);
});

test('gate: response to a CONTINUE nudge (sending-continuation) must classify COMPLETE', () => {
  const { api } = setup();
  const body = headerBody();
  const state = api.responseGate('sending-continuation', body);
  assert.strictEqual(state, 'complete', `expected complete, got ${state}`);
});

