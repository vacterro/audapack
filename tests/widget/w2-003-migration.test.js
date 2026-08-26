'use strict';

const { test } = require('node:test');
const assert = require('node:assert');
const { setup, userTurn, addTurns, runtimeFixture } = require('./helpers');

const LEGACY_KEY = 'ai_chatbuttons_auto_audit_runtime_v2';
const SESSION_KEY = 'ai_chatbuttons_auto_audit_v1';

test('W2-003: GM legacy runtime with mismatched key and no live anchor is discarded', () => {
  const { h, api } = setup();
  api.storage.gmSet(LEGACY_KEY, JSON.stringify(runtimeFixture({
    conversationKey: 'c:other-conversation',
    coreUserId: 't-999',
    stage: 'wait-second'
  })));
  const loaded = api.loadAutoRuntime('c:abc123');
  assert.strictEqual(loaded.enabled, false);
  assert.strictEqual(loaded.conversationKey, 'c:abc123');
  assert.strictEqual(loaded.coreUserId, '');
  assert.notStrictEqual(loaded.stage, 'wait-second');
});

test('W2-003: GM legacy runtime with live anchor is adopted even on key mismatch', () => {
  const { h, api } = setup();
  addTurns(h, [userTurn(h, 't-999', 'AUDIT CORE — old but present in DOM')]);
  api.storage.gmSet(LEGACY_KEY, JSON.stringify(runtimeFixture({
    conversationKey: 'c:other-conversation',
    coreUserId: 't-999',
    stage: 'wait-second'
  })));
  const loaded = api.loadAutoRuntime('c:abc123');
  assert.strictEqual(loaded.enabled, true);
  assert.strictEqual(loaded.coreUserId, 't-999');
  assert.strictEqual(loaded.conversationKey, 'c:abc123');
  assert.strictEqual(api.storage.gmGet(LEGACY_KEY, 'x'), '');
});

test('W2-003: session v1 entry without live anchor is never rebased onto a stable conversation', () => {
  const { h, api } = setup();
  h.sessionStore.set(SESSION_KEY, JSON.stringify(runtimeFixture({
    conversationKey: '',
    coreUserId: 't-888',
    stage: 'wait-second'
  })));
  const loaded = api.loadAutoRuntime('c:abc123');
  assert.strictEqual(loaded.enabled, false);
  assert.strictEqual(loaded.conversationKey, 'c:abc123');
  assert.strictEqual(loaded.coreUserId, '');
  assert.ok(h.sessionStore.get(SESSION_KEY), 'session entry must stay untouched when rejected');
});

test('W2-003: session v1 entry with live anchor is adopted and removed', () => {
  const { h, api } = setup();
  addTurns(h, [userTurn(h, 't-888', 'AUDIT SECOND WAVE — draft lineage')]);
  h.sessionStore.set(SESSION_KEY, JSON.stringify(runtimeFixture({
    conversationKey: '',
    coreUserId: 't-111',
    secondUserId: 't-888',
    stage: 'wait-performance'
  })));
  const loaded = api.loadAutoRuntime('c:abc123');
  assert.strictEqual(loaded.enabled, true);
  assert.strictEqual(loaded.secondUserId, 't-888');
  assert.strictEqual(loaded.conversationKey, 'c:abc123');
  assert.strictEqual(h.sessionStore.get(SESSION_KEY), undefined);
});

test('W2-003: session v1 draft-no-key entry is adopted on a draft conversation', () => {
  const { h, api } = setup();
  h.location.pathname = '';
  h.sessionStore.set(SESSION_KEY, JSON.stringify(runtimeFixture({
    conversationKey: '',
    coreUserId: 't-777',
    stage: 'wait-second'
  })));
  const draftKey = api.currentConversationKey();
  assert.ok(draftKey.startsWith('draft:'));
  const loaded = api.loadAutoRuntime(draftKey);
  assert.strictEqual(loaded.enabled, true);
  assert.strictEqual(loaded.coreUserId, 't-777');
  assert.strictEqual(loaded.conversationKey, draftKey);
});

test('W2-003: session v1 draft-no-key entry is rejected on a stable conversation', () => {
  const { h, api } = setup();
  h.sessionStore.set(SESSION_KEY, JSON.stringify(runtimeFixture({
    conversationKey: '',
    coreUserId: 't-666',
    stage: 'wait-second'
  })));
  const loaded = api.loadAutoRuntime('c:abc123');
  assert.strictEqual(loaded.enabled, false);
  assert.ok(h.sessionStore.get(SESSION_KEY));
});