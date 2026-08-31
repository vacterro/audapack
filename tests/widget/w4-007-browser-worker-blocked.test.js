'use strict';

const { test } = require('node:test');
const assert = require('node:assert');
const { setup } = require('./helpers');

test('P0-17: formatBrowserWorkerBlockedMessage explains clean-state-lost with next steps', () => {
  const { api } = setup();
  const msg = api.formatBrowserWorkerBlockedMessage('clean-state-lost');
  assert.ok(msg);
  assert.strictEqual(msg.code, 'clean-state-lost');
  assert.match(msg.headline, /clean/i);
  assert.match(msg.why, /draft|attachment|conversation|generation/i);
  assert.ok(Array.isArray(msg.next) && msg.next.length >= 3);
  assert.match(msg.next.join(' '), /fresh|chatgpt\.com|SEND AUDIT/i);
});

test('P0-17: formatBrowserWorkerBlockedMessage explains canonical-start-rejected', () => {
  const { api } = setup();
  const msg = api.formatBrowserWorkerBlockedMessage('canonical-start-rejected');
  assert.strictEqual(msg.code, 'canonical-start-rejected');
  assert.match(msg.headline, /START/i);
  assert.ok(msg.next.length >= 2);
});

test('P0-17: formatBrowserWorkerBlockedMessage falls back for unknown reason but keeps code', () => {
  const { api } = setup();
  const msg = api.formatBrowserWorkerBlockedMessage('mystery-reason');
  assert.strictEqual(msg.code, 'mystery-reason');
  assert.ok(Array.isArray(msg.next) && msg.next.length > 0);
});

test('P0-17: formatBrowserWorkerBlockedMessage carries project detail when provided', () => {
  const { api } = setup();
  const msg = api.formatBrowserWorkerBlockedMessage('clean-state-lost', { project: 'ACME' });
  assert.match(msg.why, /ACME/);
});
