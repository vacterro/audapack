'use strict';

const { test } = require('node:test');
const assert = require('node:assert');
const { setup, leaseFor, composerFixture } = require('./helpers');

const KEY = 'c:abc123';

function enable(h, api) {
  api.autoRuntime.enabled = true;
  api.autoRuntime.stage = 'idle';
}

test('W2-001: no token while automation disabled', async () => {
  const { h, api } = setup();
  composerFixture(h);
  const promise = api.verifyAutoLeaseForSend();
  await h.settle();
  assert.strictEqual(await promise, null);
});

test('W2-001: valid token when lease verifiably owned', async () => {
  const { h, api } = setup();
  composerFixture(h);
  enable(h, api);
  const promise = api.verifyAutoLeaseForSend();
  await h.settle();
  const token = await promise;
  assert.ok(token);
  assert.strictEqual(token.conversationKey, KEY);
  assert.ok(token.nonce);
  assert.strictEqual(api.isLeaseTokenCurrent(token), true);
});

test('W2-001: token invalid when another tab owns the lease', async () => {
  const { h, api } = setup();
  composerFixture(h);
  enable(h, api);
  leaseFor(h, KEY, 'other-tab:abc', 'other-nonce', Date.now() + 60000);
  const promise = api.verifyAutoLeaseForSend();
  await h.settle();
  assert.strictEqual(await promise, null);
});

test('W2-001: isLeaseTokenCurrent false when owner changed', async () => {
  const { h, api } = setup();
  composerFixture(h);
  enable(h, api);
  const promise = api.verifyAutoLeaseForSend();
  await h.settle();
  const token = await promise;
  assert.ok(token);
  leaseFor(h, KEY, 'other-tab:abc', 'other-nonce', Date.now() + 60000);
  assert.strictEqual(api.isLeaseTokenCurrent(token), false);
});

test('W2-001: isLeaseTokenCurrent false when nonce changed', async () => {
  const { h, api } = setup();
  composerFixture(h);
  enable(h, api);
  const promise = api.verifyAutoLeaseForSend();
  await h.settle();
  const token = await promise;
  assert.ok(token);
  leaseFor(h, KEY, api.autoInstanceId, 'different-nonce', Date.now() + 60000);
  assert.strictEqual(api.isLeaseTokenCurrent(token), false);
});

test('W2-001: isLeaseTokenCurrent false when lease expired', async () => {
  const { h, api } = setup();
  composerFixture(h);
  enable(h, api);
  const promise = api.verifyAutoLeaseForSend();
  await h.settle();
  const token = await promise;
  assert.ok(token);
  leaseFor(h, KEY, api.autoInstanceId, token.nonce, Date.now() - 1000);
  assert.strictEqual(api.isLeaseTokenCurrent(token), false);
});

test('W2-001: expired own lease is reclaimable with a fresh token', async () => {
  const { h, api } = setup();
  composerFixture(h);
  enable(h, api);
  leaseFor(h, KEY, api.autoInstanceId, 'stale-nonce', Date.now() - 1000);
  const promise = api.verifyAutoLeaseForSend();
  await h.settle();
  const token = await promise;
  assert.ok(token);
  assert.notStrictEqual(token.nonce, 'stale-nonce');
  assert.strictEqual(api.isLeaseTokenCurrent(token), true);
});

test('W2-001: missing token never current', async () => {
  const { h, api } = setup();
  composerFixture(h);
  enable(h, api);
  assert.strictEqual(api.isLeaseTokenCurrent(null), false);
  assert.strictEqual(api.isLeaseTokenCurrent({ conversationKey: KEY }), false);
  assert.strictEqual(api.isLeaseTokenCurrent({ conversationKey: KEY, nonce: 'x' }), false);
});