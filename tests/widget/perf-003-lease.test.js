'use strict';

const { test } = require('node:test');
const assert = require('node:assert');
const { setup, leaseFor, composerFixture } = require('./helpers');

const KEY = 'c:abc123';

function enable(h, api) {
  api.autoRuntime.enabled = true;
  api.autoRuntime.stage = 'idle';
}

function leaseNonce(h, api) {
  const raw = api.readAutoLease(KEY);
  return raw ? raw.nonce : null;
}

test('PERF-003: repeated claims do not rewrite storage', async () => {
  const { h, api } = setup();
  composerFixture(h);
  enable(h, api);
  assert.strictEqual(await api.claimAutoLease(), true);
  const nonce = leaseNonce(h, api);
  assert.ok(nonce);

  const writes = h.counters.gmSet;
  for (let i = 0; i < 5; i += 1) {
    assert.strictEqual(await api.claimAutoLease(), true);
  }
  assert.strictEqual(h.counters.gmSet, writes, 'fast path must not touch storage');
  assert.strictEqual(leaseNonce(h, api), nonce, 'fast path must keep the nonce');
});

test('PERF-003: renewal inside the margin extends in place with the same nonce', async () => {
  const { h, api } = setup();
  composerFixture(h);
  enable(h, api);
  assert.strictEqual(await api.claimAutoLease(), true);
  const nonce = leaseNonce(h, api);

  leaseFor(h, KEY, api.autoInstanceId, nonce, Date.now() + 5000);
  h.counters.gmSet = 0;
  assert.strictEqual(await api.claimAutoLease(), true);
  assert.strictEqual(h.counters.gmSet, 1, 'renewal must be exactly one write');
  assert.strictEqual(leaseNonce(h, api), nonce, 'renewal must preserve the fencing nonce');
});

test('PERF-003: expired self-owned lease is re-acquired with a fresh nonce', async () => {
  const { h, api } = setup();
  composerFixture(h);
  enable(h, api);
  assert.strictEqual(await api.claimAutoLease(), true);
  const nonce = leaseNonce(h, api);

  leaseFor(h, KEY, api.autoInstanceId, nonce, Date.now() - 1000);
  assert.strictEqual(await api.claimAutoLease(), true);
  assert.notStrictEqual(leaseNonce(h, api), nonce, 'expired lease must not be cached');
});

test('PERF-003: foreign-owned unexpired lease is refused without a write', async () => {
  const { h, api } = setup();
  composerFixture(h);
  enable(h, api);
  leaseFor(h, KEY, 'other-tab:x', 'foreign-nonce', Date.now() + 60000);
  h.counters.gmSet = 0;
  assert.strictEqual(await api.claimAutoLease(), false);
  assert.strictEqual(h.counters.gmSet, 0, 'foreign lease must be refused read-only');
});